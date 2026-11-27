"""Check every URL concurrently. Each URL yields exactly one CheckRow, whatever happens."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from indexer.check.feed import RecentPost, parse_feed
from indexer.errors import CheckError, ErrorCode
from indexer.net.domains import parent_domain_of
from indexer.net.fetch import FetchResult
from indexer.text.language import feed_languages
from indexer.text.topics import Post

log = logging.getLogger(__name__)


class SupportsFetch(Protocol):
    async def fetch(self, raw_url: str) -> FetchResult: ...


class SupportsLanguages(Protocol):  # indexer.text.language.LanguageModel
    def post_languages(self, texts: Sequence[str]) -> list[str | None]: ...


class SupportsTopics(Protocol):  # indexer.text.topics.TopicModel
    def topics(
        self,
        posts: Sequence[Post],
        title: str | None = None,
        subtitle: str | None = None,
        *,
        first_label: bool = True,
    ) -> list[str]: ...


@dataclass(slots=True)
class CheckRow:
    url: str
    checked_at: datetime
    final_url: str | None = None
    http_status: int | None = None
    parent_domain: str | None = None
    last_item_date: datetime | None = None
    redirected: bool = False
    first_redirect_status: int | None = None
    ip_address: str | None = None
    file_size: int | None = None
    item_count: int | None = None
    fetch_time_ms: int | None = None
    response_time_ms: int | None = None
    ttl: int | None = None
    error: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    feed_kind: str | None = None
    generator: str | None = None
    site_url: str | None = None
    self_url: str | None = None
    new_feed_url: str | None = None
    title: str | None = None
    subtitle: str | None = None
    language: str | None = None  # detected on the items: "de, en" (indexer.text.language)
    topics: str | None = None  # from the English items, most similar first (indexer.text.topics)
    media_type: str | None = None
    icon: str | None = None
    logo: str | None = None
    rights: str | None = None
    license: str | None = None
    recent_posts: tuple[RecentPost, ...] = ()  # stored for a few days (cleanup)
    item_texts: tuple[str, ...] = ()  # the newest posts' title and start: proposals, not stored
    item_tags: tuple[tuple[str, ...], ...] = ()  # each of item_texts' categories: proposals only
    detail: str | None = None  # logged, not stored


async def check_one(
    fetcher: SupportsFetch,
    url: str,
    languages: SupportsLanguages | None = None,
    topics: SupportsTopics | None = None,
) -> CheckRow:
    row = CheckRow(url=url, checked_at=datetime.now(UTC))
    try:
        row.parent_domain = parent_domain_of(url)
        res = await fetcher.fetch(url)
        row.final_url = res.final_url
        row.http_status = res.http_status
        row.redirected = res.redirected
        row.first_redirect_status = res.first_redirect_status
        row.ip_address = res.ip_address
        row.fetch_time_ms = res.fetch_time_ms
        row.response_time_ms = res.response_time_ms
        row.etag, row.last_modified = res.etag, res.last_modified
        if res.error is not None:
            row.error, row.detail = str(res.error), res.detail
            return row
        body = res.body or b""
        row.file_size = len(body)
        try:
            info = await asyncio.to_thread(parse_feed, body, res.final_url)
        except CheckError as exc:
            row.error, row.detail = str(exc.code), exc.detail
            return row
        row.item_count = info.item_count
        row.last_item_date = info.last_item_date
        row.ttl = info.ttl
        row.feed_kind = info.kind
        row.generator = info.generator
        row.site_url = info.site_url
        row.self_url = info.self_url
        row.new_feed_url = info.new_feed_url
        row.title = info.title
        row.subtitle = info.subtitle
        row.recent_posts = info.recent_posts
        row.item_texts = info.item_texts
        row.item_tags = info.item_tags
        if languages is not None:
            per_post = languages.post_languages(info.item_texts)
            labels = feed_languages(per_post)
            row.language = ", ".join(labels) or None
            # Topic names and MiniLM are English: only the English items count. Their tags are
            # evidence only, never stored; the author's first label stands in for the model only
            # when "en" is among the feed's detected languages (row.language).
            posts = [
                Post(info.item_texts[i], info.item_tags[i])
                for i, code in enumerate(per_post)
                if code == "en"
            ]
            if topics is not None and posts:
                found = await asyncio.to_thread(
                    topics.topics,
                    posts,
                    info.title,
                    info.subtitle,
                    first_label="en" in labels,  # our detected languages (row.language)
                )
                row.topics = ", ".join(found) or None
        row.media_type = info.media_type
        row.icon = info.icon
        row.logo = info.logo
        row.rights = info.rights
        row.license = info.license
    except Exception as exc:
        log.exception("internal error checking %s", url)
        row.error, row.detail = str(ErrorCode.INTERNAL_ERROR), f"{type(exc).__name__}: {exc}"
    return row


async def run_checks(
    urls: Sequence[str],
    fetcher: SupportsFetch,
    on_row: Callable[[CheckRow], Awaitable[None]] | None = None,
    languages: SupportsLanguages | None = None,
    topics: SupportsTopics | None = None,
) -> list[CheckRow]:
    async def one(url: str) -> CheckRow:
        row = await check_one(fetcher, url, languages, topics)
        if on_row is not None:
            await on_row(row)
        return row

    return list(await asyncio.gather(*(one(u) for u in urls)))
