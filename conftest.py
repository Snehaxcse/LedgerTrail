"""
Root-level conftest.py -- its mere presence here is what makes pytest add this
directory to sys.path, so `import app.*` and `import scripts.*` resolve the
same way they do when running `python -m app.startup` or `uvicorn app.main:app`
from this same directory. Shared fixtures live here rather than under tests/
so any future top-level test file can use them without a separate import.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base


@pytest.fixture
def scratch_db():
    """A fresh in-memory SQLite session, isolated from the real ledgertrail.db
    (app.database.engine/SessionLocal are never referenced here). Formalizes
    the pattern hand-written repeatedly during manual verification throughout
    this project's development -- one fixture instead of copy-pasted engine
    setup in every test file."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
