"""Optional: runs only when INDEXER_TEST_DATABASE_URL points at a migrated database."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import get_args, get_type_hints

import pytest

from indexer.check.feed import RecentPost
from indexer.check.runner import CheckRow
from indexer.db import Database
from indexer.db.models import JSON, MODELS, IPAddress, columns
from indexer.sources import InputRow

DSN = os.environ.get("INDEXER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="INDEXER_TEST_DATABASE_URL not set")


async def test_run_and_row_round_trip() -> None:
    assert DSN is not None
    db = await Database.connect(DSN)
    try:
        run_id = await db.start_run()
        row = CheckRow(
            url="https://indexer-roundtrip.example/feed",
            checked_at=datetime.now(UTC),
            final_url="https://example.com/feed.xml",
            http_status=200,
            parent_domain="example.com",
            last_item_date=datetime(2024, 1, 1, tzinfo=UTC),
            redirected=True,
            file_size=123,
            item_count=4,
            fetch_time_ms=50,
            response_time_ms=20,
            ttl=60,
            etag='"abc"',
            last_modified="Mon, 01 Jan 2024 10:00:00 GMT",
            first_redirect_status=301,
            ip_address="2606:4700::1",
            feed_kind="rss",
            generator="WordPress 6.4",
            site_url="https://example.com/",
            self_url="https://example.com/feed.xml",
            title="Example",
            language="de, en",
            topics="rust, webassembly",
            media_type="text",
            recent_posts=(RecentPost("https://example.com/a", "A", None),),
        )
        await db.insert_check(run_id, row)
        await db.finish_run(run_id)
        conn = db._conn
        cur = await conn.execute(
            "SELECT url, item_count, redirected, error, etag,"
            " first_redirect_status, host(ip_address)"
            " FROM health_checks WHERE run_id = %s",
            (run_id,),
        )
        assert await cur.fetchall() == [(row.url, 4, True, None, '"abc"', 301, "2606:4700::1")]
        cur = await conn.execute(
            "SELECT recent_posts FROM health_checks WHERE run_id = %s", (run_id,)
        )
        assert await cur.fetchone() == (
            [{"link": "https://example.com/a", "title": "A", "description": None}],
        )
        cur = await conn.execute(
            "SELECT parsed_at, feed_kind, generator, title,"
            " language, topics, media_type, site_url FROM feeds WHERE url = %s",
            (row.url,),
        )
        assert await cur.fetchone() == (
            row.checked_at,
            "rss",
            "WordPress 6.4",
            "Example",
            "de, en",
            "rust, webassembly",
            "text",
            "https://example.com/",
        )
        cur = await conn.execute(
            "SELECT finished_at IS NOT NULL FROM runs WHERE id = %s", (run_id,)
        )
        assert await cur.fetchone() == (True,)
        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (row.url,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
    finally:
        await db.close()


async def test_feed_metadata_update_rules() -> None:
    """Parsed: replace all metadata. Failed: keep it."""
    assert DSN is not None
    url = "https://indexer-meta.example/feed"
    db = await Database.connect(DSN)
    conn = db._conn

    async def feed() -> tuple[object, ...] | None:
        cur = await conn.execute(
            "SELECT title, subtitle, language, parsed_at, updated_at FROM feeds WHERE url = %s",
            (url,),
        )
        return await cur.fetchone()

    try:
        await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
        run_id = await db.start_run()
        t1, t2, t3, t4 = (datetime(2026, 11, d, tzinfo=UTC) for d in (1, 2, 3, 4))

        async def check(row: CheckRow) -> None:
            # one row per URL per run: clear the previous step's row first
            await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
            await db.insert_check(run_id, row)

        # a failure before the feed was ever parsed creates nothing
        await check(CheckRow(url=url, checked_at=t1, error="timeout"))
        assert await feed() is None

        await check(CheckRow(url=url, checked_at=t2, title="Old", subtitle="Sub", language="en"))
        assert await feed() == ("Old", "Sub", "en", t2, t2)

        # a failed check keeps the last known metadata
        await check(CheckRow(url=url, checked_at=t3, error="http_5xx"))
        assert await feed() == ("Old", "Sub", "en", t2, t2)

        # parsed again: everything replaced, dropped fields become null
        await check(CheckRow(url=url, checked_at=t4, title="New"))
        assert await feed() == ("New", None, None, t4, t4)

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
    finally:
        await db.close()


async def test_upsert_inputs_keeps_most_permissive() -> None:
    assert DSN is not None
    tiers = {"CC0-1.0": 0, "MIT": 1, "ISC": 1}
    url = "https://indexer-test.example/feed"
    other = "https://indexer-test.example/other"
    db = await Database.connect(DSN)
    try:
        await db._conn.execute("DELETE FROM inputs WHERE url IN (%s, %s)", (url, other))
        assert await db.upsert_inputs([InputRow(url, "src-mit", "MIT")], tiers) == (1, 0)
        # equal tier: no change; less permissive: no change
        assert await db.upsert_inputs([InputRow(url, "src-isc", "ISC")], tiers) == (0, 0)
        # more permissive: moved; plus one new row
        rows = [InputRow(url, "src-cc0", "CC0-1.0"), InputRow(other, "src-mit", "MIT")]
        assert await db.upsert_inputs(rows, tiers) == (1, 1)
        assert await db.upsert_inputs([InputRow(url, "src-mit", "MIT")], tiers) == (0, 0)
        cur = await db._conn.execute(
            "SELECT url, source, source_license, first_seen_at IS NOT NULL, last_checked_at"
            " FROM inputs WHERE url IN (%s, %s) ORDER BY url",
            (url, other),
        )
        assert await cur.fetchall() == [
            (url, "src-cc0", "CC0-1.0", True, None),
            (other, "src-mit", "MIT", True, None),
        ]
        await db._conn.execute("DELETE FROM inputs WHERE url IN (%s, %s)", (url, other))
    finally:
        await db.close()


async def test_batch_selection_and_last_checked() -> None:
    assert DSN is not None
    urls = [f"https://indexer-batch.example/{i}" for i in range(6)]
    db = await Database.connect(DSN)
    conn = db._conn
    try:
        await conn.execute("DELETE FROM inputs WHERE url LIKE 'https://indexer-batch.example/%'")
        await db.upsert_inputs([InputRow(u, "src", "MIT") for u in urls], {"MIT": 0})
        # 0, 1: checked within the cooldown; 2: 20 days ago; 3: 2 days ago; 4, 5: never checked
        for ago, chosen in (("1 hour", urls[:2]), ("20 days", urls[2:3]), ("2 days", urls[3:4])):
            await conn.execute(
                "UPDATE inputs SET last_checked_at = now() - %s::interval WHERE url = ANY(%s)",
                (ago, chosen),
            )
        mine = set(urls)
        picked = [u for u in await db.select_batch(100_000, 1440) if u in mine]
        # never checked first (in any order), then the longest since the last check
        assert set(picked[:2]) == {urls[4], urls[5]} and picked[2:] == [urls[2], urls[3]]
        # a shorter cooldown lets the recent ones in, last
        picked = [u for u in await db.select_batch(100_000, 30) if u in mine]
        assert set(picked[-2:]) == set(urls[:2])

        # storing a check marks the input as checked, whatever the outcome
        run_id = await db.start_run()
        row = CheckRow(url=urls[3], checked_at=datetime.now(UTC), error="timeout")
        await db.insert_check(run_id, row)
        cur = await conn.execute("SELECT last_checked_at FROM inputs WHERE url = %s", (urls[3],))
        assert await cur.fetchone() == (row.checked_at,)
        assert urls[3] not in await db.select_batch(100_000, 1440)
        # a URL that is not an input is stored without error
        await db.insert_check(
            run_id, CheckRow(url="https://not-an-input.example/", checked_at=row.checked_at)
        )

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM inputs WHERE url LIKE 'https://indexer-batch.example/%'")
    finally:
        await db.close()


async def test_concurrent_inserts_share_one_connection() -> None:
    """Checks finish concurrently and all write through one connection; every row must be
    stored (regression: interleaved transactions failed with OutOfOrderTransactionNesting)."""
    assert DSN is not None
    urls = [f"https://indexer-concurrent.example/{i}" for i in range(40)]
    db = await Database.connect(DSN)
    conn = db._conn
    try:
        await conn.execute("DELETE FROM inputs WHERE url = ANY(%s)", (urls,))
        await db.upsert_inputs([InputRow(u, "src", "MIT") for u in urls], {"MIT": 0})
        run_id = await db.start_run()
        now = datetime.now(UTC)
        rows = [
            CheckRow(url=u, checked_at=now, title="t")
            if i % 2
            else CheckRow(url=u, checked_at=now, error="timeout")
            for i, u in enumerate(urls)
        ]
        await asyncio.gather(*(db.insert_check(run_id, r) for r in rows))
        cur = await conn.execute("SELECT count(*) FROM health_checks WHERE run_id = %s", (run_id,))
        assert await cur.fetchone() == (40,)
        cur = await conn.execute(
            "SELECT count(*) FROM inputs WHERE url = ANY(%s) AND last_checked_at = %s", (urls, now)
        )
        assert await cur.fetchone() == (40,)
        cur = await conn.execute("SELECT count(*) FROM feeds WHERE url = ANY(%s)", (urls,))
        assert await cur.fetchone() == (20,)

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url = ANY(%s)", (urls,))
        await conn.execute("DELETE FROM inputs WHERE url = ANY(%s)", (urls,))
    finally:
        await db.close()


async def test_listed_feeds_rules() -> None:
    """Listed: a successful check within success_days, newest item younger than item_years."""
    assert DSN is not None
    base = "https://indexer-listed.example/"
    db = await Database.connect(DSN)
    conn = db._conn
    now = datetime.now(UTC)
    old_item = now.replace(year=now.year - 6)
    try:
        await conn.execute("DELETE FROM inputs WHERE url LIKE %s", (base + "%",))
        await db.upsert_inputs(
            [InputRow(base + "ok", "https://lists.example/a.opml", "MIT")], {"MIT": 0}
        )
        run_id = await db.start_run()
        cases = {
            "ok": CheckRow(
                url=base + "ok",
                checked_at=now,
                last_item_date=now,
                title="ok",
                recent_posts=(RecentPost(base + "ok/1", "One", "First post"),),
            ),
            "stale-item": CheckRow(
                url=base + "stale-item", checked_at=now, last_item_date=old_item
            ),
            "no-success": CheckRow(url=base + "no-success", checked_at=now, error="timeout"),
        }
        for row in cases.values():
            await db.insert_check(run_id, row)
        # an old success outside the window: parsed long ago, failing since
        await db.insert_check(
            run_id,
            CheckRow(
                url=base + "old-success",
                checked_at=now.replace(year=now.year - 1),
                last_item_date=now,
            ),
        )
        listed = {f.url: f for f in await db.listed_feeds(60, 5) if f.url.startswith(base)}
        assert set(listed) == {base + "ok"}
        ok = listed[base + "ok"]
        assert (ok.source, ok.source_license) == ("https://lists.example/a.opml", "MIT")
        assert ok.recent_posts == [
            {"link": base + "ok/1", "title": "One", "description": "First post"}
        ]
        assert ok.checked_at == now  # the check that fetched them
        await conn.execute("DELETE FROM inputs WHERE url LIKE %s", (base + "%",))
        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run_id,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        await conn.execute("DELETE FROM feeds WHERE url LIKE %s", (base + "%",))
    finally:
        await db.close()


async def test_delete_old_checks() -> None:
    """Each URL keeps its latest successful check, however old; its older successful checks go,
    and failed checks older than the cutoff. Runs left empty and older than the cutoff go too;
    feeds and inputs stay. Recent posts older than posts_days are cleared."""
    assert DSN is not None
    url = "https://indexer-cleanup.example/feed"
    lone = "https://indexer-cleanup.example/lone"  # only an old success: kept
    posts = (RecentPost("https://indexer-cleanup.example/p", "P", "d"),)
    db = await Database.connect(DSN)
    conn = db._conn
    now = datetime.now(UTC)
    old = now.replace(year=now.year - 1)
    week = now - timedelta(days=7)
    try:
        for u in (url, lone):
            await conn.execute("DELETE FROM health_checks WHERE url = %s", (u,))
            await conn.execute("DELETE FROM inputs WHERE url = %s", (u,))
        await db.upsert_inputs([InputRow(url, "src", "MIT")], {"MIT": 0})
        old_run = await db.start_run()
        await conn.execute("UPDATE runs SET started_at = %s WHERE id = %s", (old, old_run))
        await db.insert_check(old_run, CheckRow(url=url, checked_at=old, title="old"))
        await db.insert_check(old_run, CheckRow(url=lone, checked_at=old, recent_posts=posts))
        failed_old = await db.start_run()
        await conn.execute("UPDATE runs SET started_at = %s WHERE id = %s", (old, failed_old))
        await db.insert_check(failed_old, CheckRow(url=url, checked_at=old, error="timeout"))
        ok_run = await db.start_run()
        await db.insert_check(
            ok_run, CheckRow(url=url, checked_at=week, title="week", recent_posts=posts)
        )
        new_run = await db.start_run()
        await db.insert_check(
            new_run, CheckRow(url=url, checked_at=now, title="new", recent_posts=posts)
        )
        failed_new = await db.start_run()
        await db.insert_check(failed_new, CheckRow(url=url, checked_at=now, error="timeout"))

        checks, runs, cleared = await db.delete_old_checks(8, 3)
        assert checks >= 3 and runs >= 1 and cleared >= 1
        cur = await conn.execute(
            "SELECT url, run_id, recent_posts IS NOT NULL FROM health_checks"
            " WHERE url = ANY(%s) ORDER BY url, run_id",
            ([url, lone],),
        )
        assert await cur.fetchall() == [
            (url, new_run, True),  # the latest success, posts kept (less than 3 days old)
            (url, failed_new, False),  # a recent failure
            (lone, old_run, False),  # the only success, posts cleared
        ]
        cur = await conn.execute(
            "SELECT id FROM runs WHERE id = ANY(%s) ORDER BY id",
            ([old_run, failed_old, ok_run, new_run, failed_new],),
        )
        # ok_run is empty but recent; old_run still holds lone's check
        assert await cur.fetchall() == [(old_run,), (ok_run,), (new_run,), (failed_new,)]
        cur = await conn.execute("SELECT title FROM feeds WHERE url = %s", (url,))
        assert await cur.fetchone() == ("new",)
        cur = await conn.execute("SELECT last_checked_at FROM inputs WHERE url = %s", (url,))
        assert await cur.fetchone() == (now,)

        runs_made = [old_run, failed_old, ok_run, new_run, failed_new]
        await conn.execute("DELETE FROM health_checks WHERE run_id = ANY(%s)", (runs_made,))
        await conn.execute("DELETE FROM runs WHERE id = ANY(%s)", (runs_made,))
        await conn.execute("DELETE FROM feeds WHERE url = ANY(%s)", ([url, lone],))
        await conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
    finally:
        await db.close()


async def test_delete_urls_from_every_table() -> None:
    """The listed URLs go from inputs, health_checks and feeds; others stay; unknown URLs are
    simply not in the result."""
    assert DSN is not None
    gone, kept = "https://indexer-undo.example/feed", "https://indexer-undo.example/keep"
    missing = "https://indexer-undo.example/never"
    db = await Database.connect(DSN)
    conn = db._conn
    try:
        for url in (gone, kept):
            await conn.execute("DELETE FROM health_checks WHERE url = %s", (url,))
            await conn.execute("DELETE FROM feeds WHERE url = %s", (url,))
            await conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
        await db.upsert_inputs([InputRow(gone, "s", "MIT"), InputRow(kept, "s", "MIT")], {"MIT": 0})
        run = await db.start_run()
        for url in (gone, kept):
            await db.insert_check(run, CheckRow(url=url, checked_at=datetime.now(UTC), title="t"))

        assert await db.delete_urls([gone, missing]) == {
            gone: {"health_checks": 1, "feeds": 1, "inputs": 1}
        }
        for table in ("inputs", "health_checks", "feeds"):
            cur = await conn.execute(
                f"SELECT url FROM {table} WHERE url = ANY(%s) ORDER BY url", ([gone, kept],)
            )
            assert await cur.fetchall() == [(kept,)]
        assert await db.delete_urls([gone]) == {}  # again: nothing left

        await conn.execute("DELETE FROM health_checks WHERE run_id = %s", (run,))
        await conn.execute("DELETE FROM runs WHERE id = %s", (run,))
        await conn.execute("DELETE FROM feeds WHERE url = %s", (kept,))
        await conn.execute("DELETE FROM inputs WHERE url = %s", (kept,))
    finally:
        await db.close()


async def test_known_inputs() -> None:
    assert DSN is not None
    url = "https://indexer-test.example/known"
    db = await Database.connect(DSN)
    try:
        await db._conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
        assert await db.known_inputs([url, "https://indexer-test.example/x"]) == set()
        await db.upsert_inputs([InputRow(url, "src", "MIT")], {"MIT": 0})
        assert await db.known_inputs([url, "https://indexer-test.example/x"]) == {url}
        assert await db.known_inputs([]) == set()
        await db._conn.execute("DELETE FROM inputs WHERE url = %s", (url,))
    finally:
        await db.close()


async def test_drop_all_tables_drops_only_our_tables() -> None:
    # In a scratch schema, so the migrated tables the other tests use are left alone.
    assert DSN is not None
    db = await Database.connect(DSN)
    try:
        await db._conn.execute("DROP SCHEMA IF EXISTS indexer_drop_test CASCADE")
        await db._conn.execute("CREATE SCHEMA indexer_drop_test")
        await db._conn.execute("SET search_path TO indexer_drop_test")
        for table in ("inputs", "runs", "databasechangelog", "someone_elses"):
            await db._conn.execute(f"CREATE TABLE {table} (id int)")
        assert await db.drop_all_tables() == ["databasechangelog", "inputs", "runs"]
        cur = await db._conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'indexer_drop_test'"
        )
        assert [r[0] for r in await cur.fetchall()] == ["someone_elses"]
        assert await db.drop_all_tables() == []  # nothing left to drop: no error
    finally:
        await db._conn.execute("DROP SCHEMA IF EXISTS indexer_drop_test CASCADE")
        await db.close()


async def test_models_match_the_schema() -> None:
    """models.py describes every column Liquibase creates: same names, types and nullability."""
    assert DSN is not None
    pg_types = {
        int: {"smallint", "integer", "bigint"},
        str: {"text"},
        bool: {"boolean"},
        datetime: {"timestamp with time zone"},
        IPAddress: {"inet"},
        JSON: {"jsonb"},
    }
    db = await Database.connect(DSN)
    try:
        for model in MODELS:
            cur = await db._conn.execute(
                "SELECT column_name, data_type, is_nullable = 'YES' FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = %s",
                (model.TABLE,),
            )
            schema = {name: (pg_type, nullable) for name, pg_type, nullable in await cur.fetchall()}
            hints = get_type_hints(model)
            described = {}
            for name in columns(model):
                hint = hints[name]
                nullable = type(None) in get_args(hint)
                base = next((a for a in get_args(hint) if a is not type(None)), hint)
                described[name] = (base, nullable)
            assert set(described) == set(schema), model.TABLE
            for name, (base, nullable) in described.items():
                pg_type, pg_nullable = schema[name]
                assert pg_type in pg_types[base], (model.TABLE, name, pg_type)
                assert nullable == pg_nullable, (model.TABLE, name)
    finally:
        await db.close()


async def test_size_reports_the_tables_and_posts() -> None:
    assert DSN is not None
    db = await Database.connect(DSN)
    try:
        size = await db.size()
        names = [name for name, _ in size.tables]
        assert {"health_checks", "feeds", "inputs", "runs"} <= set(names)
        assert all(n > 0 for _, n in size.tables) and size.total > sum(n for _, n in size.tables)
        assert [n for _, n in size.tables] == sorted((n for _, n in size.tables), reverse=True)
        assert size.posts_checks >= 0 and size.posts_bytes >= 0
    finally:
        await db.close()
