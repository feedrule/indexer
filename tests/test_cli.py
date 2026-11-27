"""Argument parsing for the indexer commands."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from indexer.check.runner import CheckRow
from indexer.cli import parser
from indexer.cli.cleanup import render_report
from indexer.cli.load_inputs import print_failures, sources_exit_code
from indexer.cli.run import log_failure
from indexer.db.database import DatabaseSize


def test_run_from_file() -> None:
    args = parser().parse_args(["run", "data/urls.txt"])
    assert str(args.file) == "data/urls.txt" and not args.from_inputs


def test_run_from_inputs_defaults() -> None:
    args = parser().parse_args(["run", "--from-inputs"])
    assert args.from_inputs and args.file is None
    assert (args.batch, args.check_cooldown_minutes) == (500, 1440)


@pytest.mark.parametrize("argv", [["run"], ["run", "urls.txt", "--from-inputs"]])
def test_run_needs_exactly_one_source(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(argv)


def test_skipped_source_is_a_warning_not_a_failure() -> None:
    assert sources_exit_code({"a": "404"}, total=7) == 0
    assert sources_exit_code({}, total=7) == 0
    assert sources_exit_code({str(i): "x" for i in range(3)}, total=3) == 1  # nothing loaded


def test_github_actions_warning_annotation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    print_failures({"submissions": "https://x.example/a: http_4xx 404\n100%"})
    out, err = capsys.readouterr()
    assert out == (
        "::warning title=Source skipped::submissions: https://x.example/a: http_4xx 404%0A100%25\n"
    )
    assert "SKIPPED" in err


def test_no_annotation_outside_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    print_failures({"a": "gone"})
    out, err = capsys.readouterr()
    assert out == "" and "a" in err


def test_verbose_logs_failures_only(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime.now(UTC)
    caplog.set_level(logging.INFO, logger="indexer")
    log_failure(CheckRow(url="https://ok.example/feed", checked_at=now, item_count=3))
    assert caplog.text == ""
    log_failure(
        CheckRow(
            url="https://bad.example/feed",
            checked_at=now,
            error="not_a_feed",
            detail="HTML document",
        )
    )
    assert "not_a_feed" in caplog.text and "https://bad.example/feed" in caplog.text
    assert "HTML document" in caplog.text


def test_cleanup_refuses_to_cut_into_the_export_window(capsys: pytest.CaptureFixture[str]) -> None:
    from indexer.cli import main

    assert main(["cleanup", "--months", "3", "--database-url", "postgresql://unused"]) == 2
    assert "at least 7" in capsys.readouterr().err


def test_proposals_defaults() -> None:
    args = parser().parse_args(["proposals"])
    assert (args.years_ago, args.slice_mb, args.active_days, args.max_feeds) == (5, 25, 243, 20)
    assert str(args.config) == "data/sources.toml"
    assert str(args.dir) == "data/proposals" and args.report is None


def test_drop_tables_needs_yes(capsys: pytest.CaptureFixture[str]) -> None:
    from indexer.cli import main

    assert main(["drop-tables", "--database-url", "postgresql://unused"]) == 2
    assert "pass --yes" in capsys.readouterr().err


class FakeUndoDb:
    """Holds rows for two URLs, stored in their canonical form."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def delete_urls(self, urls: list[str]) -> dict[str, dict[str, int]]:
        self.asked = urls
        rows = {
            "https://a.example/feed": {"health_checks": 3, "feeds": 1, "inputs": 1},
            "https://b.example/rss": {"health_checks": 0, "feeds": 0, "inputs": 1},
        }
        return {u: rows[u] for u in urls if u in rows}


def test_undo_reports_deleted_and_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import contextlib

    from indexer import cli
    from indexer.cli import undo as undo_command

    fake = FakeUndoDb()

    @contextlib.asynccontextmanager
    async def database(_args: object) -> AsyncIterator[FakeUndoDb]:
        yield fake

    monkeypatch.setattr(undo_command, "database", database)
    undo = tmp_path / "undo.txt"
    undo.write_text(
        "# comment\nhttps://a.example/feed\n\nHTTPS://B.example:443/rss\n"
        "https://gone.example/feed\nnot a url\nhttps://a.example/feed\n"
    )
    assert cli.main(["undo", str(undo), "--database-url", "postgresql://unused"]) == 0
    out = capsys.readouterr().out
    assert "4 URLs in" in out and "2 deleted, 2 not found" in out
    assert "  https://a.example/feed  (health_checks 3, feeds 1, inputs 1)" in out
    # matched by its canonical form, reported as written
    assert "  HTTPS://B.example:443/rss  (health_checks 0, feeds 0, inputs 1)" in out
    assert out.index("not found") < out.index("  https://gone.example/feed")
    assert "  not a url" in out
    assert "https://b.example/rss" in fake.asked


def test_undo_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from indexer.cli import main

    assert main(["undo", str(tmp_path / "nope.txt"), "--database-url", "postgresql://u"]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_cleanup_size_report() -> None:
    size = DatabaseSize(
        total=312_400_000,
        tables=[("health_checks", 250_000_000), ("feeds", 40_000_000), ("inputs", 15_000_000)],
        posts_checks=3812,
        posts_bytes=6_100_000,
    )
    assert render_report(size, 300, 3) == (
        "The database takes 312.4 MB, over the 300 MB threshold."
        " Supabase's free tier allows 500 MB.\n"
        "\n"
        "| table | size (indexes included) |\n"
        "| --- | ---: |\n"
        "| health_checks | 250.0 MB |\n"
        "| feeds | 40.0 MB |\n"
        "| inputs | 15.0 MB |\n"
        "| everything else (Postgres' and Liquibase's own tables) | 7.4 MB |\n"
        "\n"
        "Recent posts: 3,812 checks hold them, 6.1 MB of health_checks (cleared after 3 days).\n"
    )
    assert render_report(size, 400, 3).startswith(
        "The database takes 312.4 MB (threshold: 400 MB)."
    )
