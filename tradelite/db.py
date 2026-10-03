from __future__ import annotations

import logging

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base

log = logging.getLogger("tradelite.db")


def normalize_url(url: str) -> str:
    """Accept the usual postgres:// and postgresql:// spellings; use the psycopg 3 driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    url = normalize_url(url)
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url == "sqlite://":
            kwargs["poolclass"] = StaticPool
        return create_engine(url, **kwargs)
    return create_engine(url, pool_pre_ping=True)


def _sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'" if isinstance(value, str) else str(value)


def upgrade_schema(engine: Engine) -> list[str]:
    """create_all() never alters an existing table, so a database created by an older version would miss
    columns added since. Add each missing column (ADD COLUMN only: nothing is dropped, renamed or rewritten).
    A NOT NULL column without a server default cannot be added safely, so that case stops with a clear error."""
    added: list[str] = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not col.nullable and col.server_default is None:
                    raise RuntimeError(f"column {table.name}.{col.name} is missing and has no default: migrate it by hand")
                default = f" DEFAULT {_sql_literal(col.server_default.arg)}" if col.server_default is not None else ""
                null = "" if col.nullable else " NOT NULL"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" '
                                  f'{col.type.compile(dialect=engine.dialect)}{default}{null}'))
                added.append(f"{table.name}.{col.name}")
    if added:
        log.warning("database upgraded, columns added: %s", ", ".join(added))
    return added


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # Run create_all for test compatibility; alembic migrations are
    # applied separately via `python -m tradelite migrate`.
    Base.metadata.create_all(engine)
    upgrade_schema(engine)
    return sessionmaker(engine, expire_on_commit=False)
