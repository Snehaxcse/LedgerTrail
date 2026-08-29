from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = "sqlite:///./ledgertrail.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def ensure_schema():
    """Create missing tables and add columns SQLite create_all will not alter."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        cols = conn.execute(text("PRAGMA table_info(approval_logs)")).fetchall()
        col_names = {row[1] for row in cols}
        if cols and "reason" not in col_names:
            conn.execute(text("ALTER TABLE approval_logs ADD COLUMN reason VARCHAR"))

        cols = conn.execute(text("PRAGMA table_info(exceptions)")).fetchall()
        col_names = {row[1] for row in cols}
        if cols and "ai_explanation" not in col_names:
            conn.execute(text("ALTER TABLE exceptions ADD COLUMN ai_explanation TEXT"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
