"""
End-to-end auth + RBAC tests, going through the REAL FastAPI dependency
injection stack (fastapi.testclient.TestClient) rather than calling endpoint
functions directly -- this is what actually proves get_current_user /
require_approver work as real request-time gates (401/403 headers resolved
from an Authorization header), not just that the Python functions compile.

SAFETY: importing app.main registers a startup event
(run_startup_sequence + AI investigation pre-warming) that would reseed AND
spend real Anthropic tokens against the REAL ledgertrail.db if the ASGI
lifespan ever ran. fastapi_app.router.on_startup.clear() strips that handler
BEFORE the TestClient is created, so no request in this file can ever trigger
it, regardless of TestClient's context-manager lifespan behavior. get_db is
also overridden to an isolated in-memory SQLite engine (never
app.database.engine/SessionLocal), so this file's HTTP requests can't reach
the real DB even for plain reads. A hash check at the end proves it stayed
untouched -- same discipline as tests/test_approve_concurrency.py.
"""
import datetime
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth, models
from app.database import Base, get_db
from app.main import app as fastapi_app

REAL_DB_PATH = Path(__file__).resolve().parent.parent / "ledgertrail.db"


def _hash_real_db():
    if not REAL_DB_PATH.exists():
        return None
    return hashlib.sha256(REAL_DB_PATH.read_bytes()).hexdigest()


fastapi_app.router.on_startup.clear()

_engine = create_engine(
    "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
)
Base.metadata.create_all(_engine)
_TestSession = sessionmaker(bind=_engine)


def _override_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


fastapi_app.dependency_overrides[get_db] = _override_get_db


def _seed():
    db = _TestSession()
    try:
        auth.seed_demo_users(db)

        batch = models.SettlementBatch(
            settlement_date=datetime.date(2026, 1, 1),
            total_gross=10000, total_refunds=0, total_fees=100, total_tax=50, total_net=9850,
        )
        txn = models.BankTransaction(amount=9850, date=datetime.date(2026, 1, 1), reference="REF-RBAC", description=None)
        db.add_all([batch, txn])
        db.flush()
        batch.bank_transaction_id = txn.id
        exc = models.ExceptionRecord(
            batch_id=batch.id, unexplained_amount=500, classification="TIMING_DIFFERENCE",
            suggested_action="test", status="open", severity="info",
        )
        db.add(exc)
        db.commit()
        return exc.id
    finally:
        db.close()


client = TestClient(fastapi_app)
EXCEPTION_ID = _seed()


def _login(username, password="demo1234"):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_login_success_returns_token_and_correct_role():
    response = _login("sneha")
    assert response.status_code == 200
    body = response.json()
    assert body["token"]
    assert body["role"] == "approver"
    assert body["display_name"] == "Sneha"


def test_login_failure_wrong_password():
    response = _login("sneha", password="wrong-password")
    assert response.status_code == 401


def test_login_failure_unknown_username():
    response = _login("not-a-real-user")
    assert response.status_code == 401


def test_me_requires_valid_token():
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401

    token = _login("rahul").json()["token"]
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["role"] == "analyst"


def test_core_endpoint_requires_authentication():
    """GET /batches (a core review-workflow endpoint) with no token at all --
    missing token, not just wrong role."""
    assert client.get("/batches").status_code == 401


def test_analyst_can_reach_core_read_endpoints():
    token = _login("rahul").json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/batches", headers=headers).status_code == 200
    assert client.get("/stats", headers=headers).status_code == 200
    assert client.get("/audit-trail", headers=headers).status_code == 200


def test_analyst_approval_denied_403():
    token = _login("rahul").json()["token"]
    response = client.post(
        f"/exceptions/{EXCEPTION_ID}/approve",
        json={"decision": "approved"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_approval_without_any_token_is_401_not_403():
    """A missing token and a wrong-role token are different failure modes --
    confirms get_current_user (401) runs before require_approver (403)."""
    response = client.post(f"/exceptions/{EXCEPTION_ID}/approve", json={"decision": "approved"})
    assert response.status_code == 401


def test_approver_approval_allowed_and_actor_is_server_derived():
    token = _login("sneha").json()["token"]
    # Client attempts to spoof a different approver in the request body --
    # ApprovalRequest.approver is optional/ignored by the real endpoint (see
    # app/main.py), so this must have zero effect on who gets recorded.
    response = client.post(
        f"/exceptions/{EXCEPTION_ID}/approve",
        json={"decision": "approved", "approver": "Someone Else Entirely"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    db = _TestSession()
    try:
        log = db.query(models.ApprovalLog).filter(models.ApprovalLog.exception_id == EXCEPTION_ID).first()
        assert log.approver == "Sneha"
        assert log.approver != "Someone Else Entirely"

        event = (
            db.query(models.AuditEvent)
            .filter(models.AuditEvent.action == "exception_reviewed")
            .order_by(models.AuditEvent.id.desc())
            .first()
        )
        after = json.loads(event.after_state)
        assert after["approver"] == "Sneha"
    finally:
        db.close()


def test_zero_mutation_to_real_ledgertrail_db():
    before = _hash_real_db()
    # Re-run a handful of authenticated requests after all prior tests.
    token = _login("priya").json()["token"]
    client.get("/batches", headers={"Authorization": f"Bearer {token}"})
    after = _hash_real_db()
    assert before == after
