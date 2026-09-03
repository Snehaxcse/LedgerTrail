"""
Minimal authentication + RBAC. Deliberately small by design (see the P1
spec): no OAuth, no password reset, no refresh tokens, no permission-
management UI. Flow: username + password -> backend verification (PBKDF2,
stdlib hashlib, no new dependency) -> server-issued opaque session token ->
every protected request carries that token in an Authorization: Bearer
header -> the server looks up the session and derives identity + role from
it. The client NEVER supplies identity or role directly to a protected
endpoint -- see app/main.py's approve_exception, which uses
current_user.display_name for ApprovalLog/AuditEvent, not anything from the
request body.

Two roles only: "analyst" (view/investigate/inspect evidence and audit/
transparency) and "approver" (everything an analyst can do, plus
approve/reject). DEMO_CREDENTIALS below are synthetic, not real accounts --
same "Simulated"/demo framing already used elsewhere in this app (see
app/main.py's DEMO_APPROVERS, which this does NOT replace -- that dict still
powers the UNRELATED holdout-sandbox concurrency demo's own simulated
approver picker, which has never claimed to be real authentication and stays
exactly as it was).

SCOPE (documented deliberately, not an oversight): get_current_user is
required on the core exception-review workflow -- batches, batch detail,
exceptions, evidence, explain, investigate, approve, audit-trail,
transparency, stats (see app/main.py). It is NOT applied to the standalone
demo/proof pages (hero-case agent demo, Razorpay ingestion replay, held-out
evaluation, holdout-sandbox demos, bank statement/narration-verify, trend,
data-sources, NL query) -- those are supplementary or self-contained demos,
not part of the workflow whose financial decisions this auth model exists to
gate, and gating them would be a much larger, riskier change for no spec-
mandated benefit.
"""
import datetime
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app import models
from app.database import get_db

PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 16
TOKEN_BYTES = 32

# Synthetic demo credentials, clearly marked as such -- shown directly in the
# login page's UI (frontend/src/pages/Login.jsx) so a judge can log in
# without hunting for them elsewhere. Same password for all three, on
# purpose: this is a demo of ROLE enforcement, not a password-strength demo.
DEMO_CREDENTIALS = [
    {"username": "sneha", "password": "demo1234", "role": "approver", "display_name": "Sneha", "job_title": "Finance Analyst"},
    {"username": "rahul", "password": "demo1234", "role": "analyst", "display_name": "Rahul", "job_title": "Reconciliation Analyst"},
    {"username": "priya", "password": "demo1234", "role": "approver", "display_name": "Priya", "job_title": "Settlements Lead"},
]


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def hash_new_password(password: str) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex) for a brand-new credential."""
    salt = secrets.token_bytes(SALT_BYTES)
    return _hash_password(password, salt), salt.hex()


def verify_password(password: str, password_hash_hex: str, salt_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    candidate = _hash_password(password, salt)
    # Constant-time comparison -- a plain == would leak timing information
    # about how many leading hex characters matched.
    return hmac.compare_digest(candidate, password_hash_hex)


def seed_demo_users(db: Session):
    """Idempotent: only inserts users that don't already exist by username.
    Called from app/startup.py on every boot, but -- unlike the
    reconciliation dataset -- NOT preceded by a wipe, so already-existing
    accounts (and any session tokens issued against them) survive a
    restart untouched."""
    for cred in DEMO_CREDENTIALS:
        existing = db.query(models.DemoUser).filter(models.DemoUser.username == cred["username"]).first()
        if existing is not None:
            continue
        password_hash, salt = hash_new_password(cred["password"])
        db.add(models.DemoUser(
            username=cred["username"],
            password_hash=password_hash,
            password_salt=salt,
            role=cred["role"],
            display_name=cred["display_name"],
            job_title=cred["job_title"],
        ))
    db.commit()


def create_session(db: Session, user: models.DemoUser) -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(models.UserSession(token=token, user_id=user.id, created_at=datetime.datetime.now()))
    db.commit()
    return token


@dataclass
class AuthenticatedUser:
    id: int
    username: str
    role: str
    display_name: str
    job_title: str


def _resolve_token(db: Session, authorization: Optional[str]) -> Optional[AuthenticatedUser]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    if not token:
        return None
    session = db.query(models.UserSession).filter(models.UserSession.token == token).first()
    if session is None:
        return None
    user = db.query(models.DemoUser).filter(models.DemoUser.id == session.user_id).first()
    if user is None:
        return None
    return AuthenticatedUser(
        id=user.id, username=user.username, role=user.role,
        display_name=user.display_name, job_title=user.job_title,
    )


def get_current_user(
    authorization: Optional[str] = Header(None), db: Session = Depends(get_db)
) -> AuthenticatedUser:
    """FastAPI dependency: 401 if the request carries no valid session token.
    This is the ONLY way a protected endpoint learns who is calling --
    nothing about identity is ever read from the request body."""
    user = _resolve_token(db, authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated -- log in and retry with a valid session token.")
    return user


def require_approver(current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """FastAPI dependency: 403 if the authenticated user's role isn't
    "approver". Layered on top of get_current_user, so an unauthenticated
    request still gets 401 (not authenticated), not 403 (authenticated but
    disallowed) -- the two are different failure modes and this keeps them
    distinct, same principle as the rest of this app's error handling."""
    if current_user.role != "approver":
        raise HTTPException(
            status_code=403,
            detail=f"'{current_user.display_name}' is an analyst and cannot approve or reject exceptions.",
        )
    return current_user
