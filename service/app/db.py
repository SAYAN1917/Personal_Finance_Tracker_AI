"""Database engine and session management.

Idempotent initialization: safe to call from multiple threads / processes
(uvicorn + bot) without double-initializing (Prototype Bug G).
"""

from contextlib import contextmanager

from sqlalchemy import create_engine, text
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


def _alembic_head() -> str | None:
    """Current head revision from alembic scripts, or None if unavailable."""
    try:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = str(Path(__file__).resolve().parents[1] / "alembic.ini")
        script = ScriptDirectory.from_config(Config(ini))
        heads = script.get_heads()
        return heads[0] if heads else None
    except Exception:  # noqa: BLE001 - migrations check must never crash /ready
        return None


def check_migrations(session) -> str:
    """Readiness: report 'uninitialized' / 'pending' / 'up_to_date'.

    Dev mode (create_all) has no alembic_version table -> treated as
    up_to_date once the schema exists. Prod runs alembic, so the version
    table must match the head revision.
    """
    from sqlalchemy import inspect

    tables = set(inspect(session.bind).get_table_names())
    if "transactions" not in tables:
        return "uninitialized"
    if "alembic_version" not in tables:
        return "up_to_date"  # dev create_all mode
    head = _alembic_head()
    version = session.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar()
    if head and version and version == head:
        return "up_to_date"
    return "pending"
