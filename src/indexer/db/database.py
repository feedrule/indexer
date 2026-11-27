"""Postgres reads and writes, in SQL through psycopg: the statements are in sql/, one per file,
except the INSERTs built from models.py. Liquibase owns the schema (liquibase/changelog); this
module never creates it."""

import asyncio
import functools
import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from typing import Self

import psycopg
from psycopg import sql
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from indexer.check.feed import STORED_POST_FIELDS
from indexer.check.runner import CheckRow
from indexer.db.models import Feed, HealthCheck, Input, IPAddress, Model, Run, columns, values
from indexer.publish.export import ListedFeed
from indexer.sources import InputRow

DEFAULT_DATABASE_URL = "postgresql://indexer@127.0.0.1:5432/indexer"  # password: $PGPASSWORD
# Every table this project creates (liquibase/changelog), plus Liquibase's own: what drop-tables
# drops.
OWN_TABLES = (
    "health_checks",
    "runs",
    "feeds",
    "inputs",
    "databasechangelog",
    "databasechangeloglock",
)


# The columns a CheckRow fills under the same name.
_CHECK_FIELDS = [
    c
    for c in columns(HealthCheck, writable=True)
    if c not in ("run_id", "ip_address", "recent_posts")
]
FEED_METADATA = [c for c in columns(Feed) if c not in ("url", "updated_at", "parsed_at")]


# Tables `undo` deletes a URL from, in deletion order.
UNDO_TABLES = ("health_checks", "feeds", "inputs")


@dataclass(frozen=True, slots=True)
class DatabaseSize:
    total: int  # bytes, the whole database (what Supabase's limit counts)
    tables: list[tuple[str, int]]  # this project's tables (not Liquibase's), largest first
    posts_checks: int  # checks holding recent_posts
    posts_bytes: int  # what those posts take, as stored


class Database:
    def __init__(self, conn: psycopg.AsyncConnection) -> None:
        self._conn = conn
        # One connection serves every concurrent check. A connection runs one transaction at a
        # time, so concurrent writers must take turns; otherwise their transactions interleave
        # and psycopg aborts them (OutOfOrderTransactionNesting).
        self._lock = asyncio.Lock()

    @classmethod
    async def connect(cls, dsn: str) -> Self:
        return cls(await psycopg.AsyncConnection.connect(dsn, autocommit=True))

    async def close(self) -> None:
        await self._conn.close()

    async def start_run(self) -> int:
        return (await self._insert(Run(started_at=datetime.now(UTC)))).id

    async def finish_run(self, run_id: int) -> None:
        params = {"finished_at": datetime.now(UTC), "run_id": run_id}
        await self._conn.execute(_query("finish_run"), params)

    async def select_batch(self, size: int, cooldown_minutes: int) -> list[str]:
        """The `size` most outdated inputs: never checked first, then the longest since their
        last check (ties in random order). An input checked in the last `cooldown_minutes` is
        never picked, so no URL is checked more often than that."""
        params = {"size": size, "cooldown_minutes": cooldown_minutes}
        cur = await self._conn.execute(_query("select_batch"), params)
        return [row[0] for row in await cur.fetchall()]

    async def known_inputs(self, urls: Sequence[str]) -> set[str]:
        """Those of `urls` that are already inputs."""
        cur = await self._conn.execute(_query("known_inputs"), {"urls": list(urls)})
        return {row[0] for row in await cur.fetchall()}

    async def insert_check(self, run_id: int, row: CheckRow) -> None:
        """Store one check, in one transaction:
        - the per-check health row;
        - the feed's metadata in `feeds`: replaced when the feed parsed, untouched when the
          check failed;
        - `inputs.last_checked_at`, if the URL is an input (any outcome: a failing feed waits
          its turn like the rest)."""
        async with self._lock, self._conn.transaction():
            await self._insert(_health_check(run_id, row))
            if row.error is None:
                # Descriptive metadata lives once per feed, not on every check. A parsed feed
                # replaces all of it: a field the feed dropped becomes null.
                await self._insert(_feed(row), replace_on="url")
            params = {"checked_at": row.checked_at, "url": row.url}
            await self._conn.execute(_query("mark_checked"), params)

    async def upsert_inputs(
        self, rows: Sequence[InputRow], license_tiers: Mapping[str, int]
    ) -> tuple[int, int]:
        """Load rows into `inputs` in one transaction. Returns (inserted, re-attributed)."""
        licenses = list(license_tiers)
        params = {"licenses": licenses, "tiers": [license_tiers[x] for x in licenses]}
        async with self._lock, self._conn.transaction(), self._conn.cursor() as cur:
            await cur.execute(_query("create_inputs_load"))
            async with cur.copy(_query("copy_inputs_load")) as copy:
                for row in rows:
                    await copy.write_row((row.url, row.source, row.source_license))
            await cur.execute(_query("upsert_inputs"), params)
            changed = [r[0] for r in await cur.fetchall()]
        inserted = sum(changed)
        return inserted, len(changed) - inserted

    async def listed_feeds(self, success_days: int, item_years: int) -> list[ListedFeed]:
        """Feeds with a successful check in the last `success_days` days whose newest item, as
        of their latest successful check, is less than `item_years` years old."""
        cur = self._conn.cursor(row_factory=class_row(ListedFeed))
        params = {"success_days": success_days, "item_years": item_years}
        await cur.execute(_query("listed_feeds"), params)
        return await cur.fetchall()

    async def drop_all_tables(self) -> list[str]:
        """Drop every table this project owns, Liquibase's bookkeeping included, so the next
        migration rebuilds an empty schema. Only the tables named in OWN_TABLES: other tables in
        the database (Supabase's, another app's) are never touched. Returns those that existed."""
        async with self._lock, self._conn.transaction():
            cur = await self._conn.execute(_query("own_tables"), {"tables": list(OWN_TABLES)})
            existing = [row[0] for row in await cur.fetchall()]
            if existing:
                await self._conn.execute(
                    sql.SQL(_query("drop_tables")).format(
                        tables=sql.SQL(", ").join(sql.Identifier(t) for t in existing)
                    )
                )
            return existing

    async def delete_urls(self, urls: Sequence[str]) -> dict[str, dict[str, int]]:
        """Delete these exact URLs from health_checks, feeds and inputs, in one transaction.
        Returns, per URL that was found anywhere, the rows deleted from each table."""
        found: dict[str, dict[str, int]] = {}
        async with self._lock, self._conn.transaction():
            for table in UNDO_TABLES:
                cur = await self._conn.execute(
                    sql.SQL(_query("delete_urls")).format(table=sql.Identifier(table)),
                    {"urls": list(urls)},
                )
                for (url,) in await cur.fetchall():
                    counts = found.setdefault(url, dict.fromkeys(UNDO_TABLES, 0))
                    counts[table] += 1
        return found

    async def delete_old_checks(self, months: int, posts_days: int) -> tuple[int, int, int]:
        """Keep only each URL's most recent successful check (however old) and the failed
        checks of the last `months`; delete runs older than that with no checks left; clear
        the recent posts of checks older than `posts_days`. feeds and inputs are untouched.
        Returns (checks deleted, runs deleted, checks whose posts were cleared)."""
        async with self._lock, self._conn.transaction():
            cur = await self._conn.execute(_query("delete_old_checks"), {"months": months})
            checks = cur.rowcount
            cur = await self._conn.execute(_query("delete_empty_runs"), {"months": months})
            runs = cur.rowcount
            cur = await self._conn.execute(_query("clear_old_posts"), {"days": posts_days})
            return checks, runs, cur.rowcount

    async def size(self) -> DatabaseSize:
        """How big the database is, and what takes the space."""
        cur = await self._conn.execute(_query("database_size"))
        (total,) = await cur.fetchone() or (0,)
        tables = [t for t in OWN_TABLES if not t.startswith("databasechangelog")]  # ~0 bytes
        cur = await self._conn.execute(_query("table_sizes"), {"tables": tables})
        sizes = [(name, size) for name, size in await cur.fetchall()]
        cur = await self._conn.execute(_query("posts_size"))
        posts_checks, posts_bytes = await cur.fetchone() or (0, 0)
        return DatabaseSize(total, sizes, posts_checks, posts_bytes)

    async def _insert[R: (Run, HealthCheck, Input, Feed)](
        self, row: R, *, replace_on: str | None = None
    ) -> R:
        """Store `row` and return it as stored, with the columns the database fills (ids,
        defaults). With `replace_on`, a row that already has that column's value is replaced."""
        cur = self._conn.cursor(row_factory=class_row(type(row)))
        # A list is a JSONB column's value (models.JSON); psycopg would send it as an array.
        params = {k: Jsonb(v) if isinstance(v, list) else v for k, v in values(row).items()}
        await cur.execute(_insert_sql(type(row), replace_on), params)
        stored = await cur.fetchone()
        assert stored is not None
        return stored


@functools.cache
def _query(name: str) -> str:
    """The statement in sql/<name>.sql."""
    return files("indexer.db").joinpath("sql", f"{name}.sql").read_text()


def _insert_sql(model: Model, replace_on: str | None) -> sql.Composed:
    """INSERT INTO <table> (<writable columns>) VALUES (%(column)s, ...)
    [ON CONFLICT (<replace_on>) DO UPDATE SET <column> = EXCLUDED.<column>, ...] RETURNING *"""
    cols = columns(model, writable=True)
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(model.TABLE),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        sql.SQL(", ").join(map(sql.Placeholder, cols)),
    )
    if replace_on is not None:
        stmt += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET {}").format(
            sql.Identifier(replace_on),
            sql.SQL(", ").join(
                sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c))
                for c in cols
                if c != replace_on
            ),
        )
    return stmt + sql.SQL(" RETURNING *")


def _inet(value: str | None) -> IPAddress | None:
    try:
        return ipaddress.ip_address(value.split("%")[0]) if value else None
    except ValueError:
        return None


def _health_check(run_id: int, row: CheckRow) -> HealthCheck:
    fields = {name: getattr(row, name) for name in _CHECK_FIELDS}
    posts = (
        [{k: getattr(p, k) for k in STORED_POST_FIELDS} for p in row.recent_posts]
        if row.error is None
        else []
    )
    return HealthCheck(
        run_id=run_id, ip_address=_inet(row.ip_address), recent_posts=posts or None, **fields
    )


def _feed(row: CheckRow) -> Feed:
    metadata = {name: getattr(row, name) for name in FEED_METADATA}
    return Feed(url=row.url, updated_at=row.checked_at, parsed_at=row.checked_at, **metadata)
