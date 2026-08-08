"""Database engine and session management.

Idempotent initialization: safe to call from multiple threads / processes
(uvicorn + bot) without double-initializing (Prototype Bug G).
"""

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
    return _engine


def init_db() -> None:
    """Create all tables. Idempotent - safe to call repeatedly."""
    from app import models  # noqa: F401  ensure models are registered

    engine = get_engine()
    models.Base.metadata.create_all(bind=engine)


def reset_db() -> None:
    """Reset cached engine/sessionmaker (used by tests between runs)."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None


def get_sessionmaker():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    Session = get_sessionmaker()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
