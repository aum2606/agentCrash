"""Engine construction. Pool is small: the MVP allows 4 concurrent runs and light API traffic."""

from sqlalchemy import URL, Engine, create_engine


def make_engine(url: URL) -> Engine:
    return create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
