def have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


HAVE_SQLALCHEMY = have("sqlalchemy")
HAVE_FASTAPI = have("fastapi") and have("httpx")
HAVE_PIKA = have("pika")


def sqlite_store():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from codeforge_shared.models import Base
    from codeforge_shared.store import SqlStore

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return SqlStore(sessionmaker(engine, expire_on_commit=False))
