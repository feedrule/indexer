# indexer (Maintained Awesome RSS)

A self-maintaining awesome list of RSS/Atom feeds: collect feed URLs (curated lists, submissions,
Common Crawl), check them regularly and securely, and publish the active ones by topic. README.md is for people
looking for feeds (ends with a brief "How it works"); DEVELOP.md has the technical documentation (no duplication between the two); this file holds the rules and decisions to respect when changing code.

Runs locally against the compose Postgres; deployed as GitHub Actions workflows writing to Supabase.

## Pipeline

| stage | command | compose service | workflow |
| --- | --- | --- | --- |
| inputs from lists | `indexer load-inputs data/sources.toml` | `input` | `input.yml` (daily; on push to develop/main changing `data/sources.toml` or `data/*.txt`, except `undo.txt`) |
| old CC feeds still active, proposed on a branch | `indexer proposals` | `proposals` | `proposals.yml` (Wednesdays) |
| health checks | `indexer run --from-inputs` (or `run FILE`) | `check` | `check.yml` (every 3 h, 500 URLs, 3 h timeout) |
| publish | `indexer export` | `export` | `publish.yml` (after each successful check run; on push to develop changing src/indexer/publish/; Mondays as fallback; manual too. Re-enabled 2026-11-19 at the user's request) |
| retention | `indexer cleanup` | `cleanup` | `cleanup.yml` (daily since 2026-11-27, was Sundays) |
| embedding sets (`data/embeddings/`) | `indexer embeddings [NAME ...]` | `embeddings` | `embeddings.yml` (on push to develop changing a set's source, e.g. `data/topics/topics.txt`, or `Dockerfile`; manual) |
| sentence clusters (`data/clusters/posts.{npy,json,md}` on the `binaries` branch) | `indexer clusters --posts DIR` | `clusters` | `clusters.yml` (Mondays; manual) |
| delete listed URLs | `indexer undo data/undo.txt` | `undo` | `undo.yml` (on push to develop changing `data/undo.txt`; manual) |
| start over (all data) | `indexer drop-tables --yes` | `drop-tables` | `drop-tables.yml` (manual, typed confirmation) |

## Decisions made with the user (keep them)

- **Sources are unpinned and API-free**: `data/sources.toml` lists raw file URLs on a branch (`urls`) and/or repo
  paths (`files`, relative to sources.toml), with a stated SPDX license that must be in `[licenses] ranking`.
  Each load reads the lists as they are. A failing source is skipped (stderr, plus a `::warning::` annotation in
  Actions); exit 1 only if every source failed.
- **inputs is append-only**: nothing deletes inputs (except data changesets the user asked for, like the former 009 for
  YouTube/Reddit, `drop-tables`, the user-requested manual reset that drops every table, only names in
  `db.OWN_TABLES`, and `undo`, asked by the user on 2026-11-19: deletes the exact URLs in `data/undo.txt`, as written
  and in canonical form, from inputs, health_checks and feeds in one transaction, and reports deleted (rows per table)
  and not found. A URL still in a source comes back on the next load). A URL moves only to a strictly more permissive source. That is what makes unpinned sources safe.
- **No quality filtering of inputs.** Inputs are candidates; publishing is a separate responsibility.
  **Agree any selection or quality heuristic with the user before implementing it.** (`discover`, which
  inserted the newest crawl's feeds unfiltered, was removed on 2026-11-16: `indexer proposals` replaces it.)
- **One name** (asked 2026-11-27): the pipeline once called survivors is `proposals` everywhere (command, workflow,
  package, service, proposal branches, files, source); "survivors" only describes the rule (old feeds still active). Feeds loaded
  before the move keep their inputs.source (.../data/survivors); `site.source_of` maps it to the `data/proposals/` entry.
- **Proposals branch** (asked 2026-11-27, so develop doesn't grow with proposals: 200k more feeds are to come from them):
  orphan branch `proposals` with `data/proposals/` (moved from develop's `data/survivors/` and `data/training.toml`):
  `<yyyymm>/<dd-HHMMSS>.{yaml,opml}` and `training.toml`. The source `proposals` (format `proposals`, `files =
  ["proposals/*/*.yaml"]`) reads the files and loads only `accept: true` (agreed 2026-11-27: no accepted.txt or lists
  pipeline; an index can come back when reading every file gets slow). The branch has no workflows, so a merge into it
  starts nothing: the daily input run picks it up. Workflows (input, proposals, clusters) check the branch out into `./proposals/` and mount only its
  `data/proposals/` as the container's `data/proposals/` (no .git, no token in the container); commits happen on the
  runner with hooks off. Locally: `git worktree add proposals origin/proposals`. Old `survivors-*` branches are ignored.
- `indexer proposals` **proposes, never inserts**: writes a new file `data/proposals/<yyyymm>/<dd-HHMMSS>.yaml` (UTC) per run on the `proposals` branch (asked 2026-11-21,
  replacing the single `data/survivors.txt`: no huge file, no conflicts between waiting branches; CC ToU license; `files` entries in
  sources.toml may be glob patterns, sorted, none matching is fine, and format `proposals` still reads such files) and
  every proposals.yml run pushes a new branch `proposals-<UTC timestamp>` (the file's name), an orphan with one commit
  holding only the run's .yaml, .opml and .summary.md (asked 2026-11-28; never a reused branch, so old proposals can't be merged
  by mistake; merged with `git merge --squash --allow-unrelated-histories`). **No pull requests** (asked 2026-11-28: a PR keeps its commits
  on GitHub for good, so deleting branches or compacting `proposals` could never shrink the repository): the run's
  summary goes in `<dd-HHMMSS>.summary.md` beside the .yaml (and the run's page); the operator reviews on the branch,
  squash-merges it into `proposals` by hand and deletes it. Skips URLs already in that branch's files (accepted or not) or in inputs.
  Untrusted titles/categories/links: one line in the file (`files._one_line`); none of it goes in the summary.
  The file is YAML (asked 2026-11-22, replacing comment blocks in .txt files, converted once): a comment header, then one
  document per feed separated by `---`, fields easy to parse: url, `accept: false` by default, `learn: true` by default (asked 2026-11-28: `learn: false` keeps the feed out of the proposal filter's training; no key counts as true), title, site (the feed's website, http(s) only; asked 2026-11-27), language, last_post, future_dated (true/false, always written: some item dated more than a day ahead; asked 2026-11-28; `FeedInfo.future_dated`, never stored),
  about, posts_read and categories (the authors' top 8 with counts, `run._top_categories` from `FeedInfo.item_tags`, never
  stored), authors (who wrote the posts read: names only, never an email address; top 5 with counts, `run._top_authors` from `FeedInfo.item_authors`, never stored; asked 2026-11-28; not in the proposal model's text), found_in (the WAT file), recent_posts (3: link, date (the item's newest date, a UTC day; asked 2026-11-27, also stored in `recent_posts`), title, links (the other sites the post links to, registrable domains with counts, top 5) and `sponsored: true` (strict evidence only: rel=sponsored, affiliate network domains, Amazon `tag=`, a disclosure; `check/post_links.py`; asked 2026-11-28, per post only, no feed totals; never stored), description as a list of lines ≤ 60 characters for phones (the longest of the item's description fields, so full content beats an excerpt; asked 2026-11-28, stored that way too; language and topics still read the first);
  `FeedInfo.recent_posts`, never stored or fetched); lists and mappings in block style, one item per line. Written with
  `yaml.safe_dump` (quotes anything that would parse as something else, no aliases), read with `yaml.safe_load_all`
  (`sources.proposal_documents`). A reviewer sets `accept: true` on the feeds they want; only those are loaded.
  Proposals left at false count as known (not proposed again).
  **Model review** (asked 2026-11-28): `prompts/review-proposals.md` has a model (Haiku) judge a file with **no tools**,
  answering one line per feed (`accept - <url>`, `reject <reason> <url>`, `unsure - <url>`; unsure = not enough
  information: `learn: false`, accept false; reject keeps learn true with a reason); `indexer review FILE ANSWER`
  (`proposals/review.py`) applies it, editing only accept/learn/reason lines, all or nothing, the answer being untrusted.
  Wanted: relevant to read, mainstream or niche, not hyperlocal (a primary school, an ordinary church).
  Summary file (was the PR body): the edit link, the OPML link, the merge commands and the run's summary only, no feed listed (asked 2026-11-21: the
  feeds are reviewed in the file). All proposals as OPML go in a
  `<dd-HHMMSS>.opml` beside the .yaml (committed with it; not a source), linked from the summary for download
  (moved out of the report at the user's request on 2026-11-21); max-feeds default 20 (was 50).
  Agreed filters: crawl closest to 5 years ago, first 25 MB of one random WAT file (one range
  request, random each run, no state; one file because the yield per MB is the same as several smaller slices); every feed link of every page (2026-11-21: one random link per page, agreed 2026-11-20, was dropped once the URL rules removed most junk), session params stripped
  (`urls.strip_session`), every http link tried as https, only links shaped like a feed URL (`feed_links._looks_like_a_feed`:
  length, depth, rejected and accepted patterns; the user's rules, 2026-11-21, plus the main feeds of Blogger, WordPress `?feed=rss2`, Joomla, SPIP and `backend.php`; no `/web-stories/`; no forums or wikis ("forum"/"forums" as a host label or path segment, `feed.php`, `app.php/feed`, `syndication.php`, `type=rss;action`, BuddyPress `/activity/feed`, Invision `/rss/public` and `/rss/<n>-`, Digital Commons `/recent.rss`, agreed 2026-11-21; the same rules (`feed_links.is_unwanted`) are applied again to the URL after redirects); no Freenom TLDs (`feed_links.FREE_TLDS`: tk, ml, ga, cf, gq); WordPress `/feed/atom/`, `/feed/rss2/`, `/feed/rss/`, `/feed/rdf/` read as `/feed/` (so a known feed in another format is skipped); URL-only rules live in feed_links.py, rules on the checked feed in filters.py; not the page's own `<page>/feed` when the page offers another), which also rule out WordPress category/tag/author feeds and comment feeds (WordPress `/comments/feed/`,
  `?feed=comments-*`; Blogger `/feeds/comments/`, `/feeds/<id>/comments/`; agreed 2026-11-20)
  (skipped unchecked); check the rest now, at most 10 new URLs per site, shortest first (asked 2026-11-21); of the feeds that pass on one site (host without `www.`, not the registrable domain: shared hosts like fc2.com or free.fr), keep the shortest URL (ties by name); insert only feeds whose newest item is
  from the last 243 days (~8 months), are **in English** (`en` the only language detected on their items: among them, agreed
  2026-11-18; only, agreed 2026-11-21; what the feed declares is not read), **have a title** (a description is not required: that rule, removed 2026-11-21, would drop 15% of listed feeds), have a recent post with a title or text, no business or organisation word in title or description (`filters._BUSINESS_WORDS`: services, agency, shop, company, law firm, radio station, parish, ...; ~0.7% of listed feeds would hit), no "Jobs from" description (job boards), have no recent post
  linking to a wiki edit (`do=diff`, `rev=`, `oldid=`, `doku.php`, `index.php?title=`) or carrying `utm_` parameters,
  no "appeared first on" / "first appeared on" in their posts, no WordPress sample post ("Welcome to WordPress. This is your first post"), no two recent posts with the same title and text, no two recent post descriptions starting "Discover/Learn/Explore/Understand/Uncover/Find out" (SEO meta descriptions), no two recent post descriptions starting with their own title (generated text), no "article appeared in" (reposts) (simple rules agreed 2026-11-21 on 60 reviewed
  proposals: they caught ~19 of 38 rejected and ~6 of 22 accepted; false positives are fine, Common Crawl is vast), and are
  **served over https today** (inserted as the URL after redirects; links are tried as https, and one that
  redirects back to plain http is skipped). Rejected as too complex: detecting one feed in several formats by its items (only the URL rules above).
  Still open: a takeover guard, storing these checks (not stored now).
- `[exclude] domains` (youtube.com, reddit.com) is matched by registrable domain and applied at load and in proposals.
- `[commoncrawl] exclude_keywords` (adult / sexual-violence words; gambling words added 2026-11-17 after casino spam in a sample), **Common Crawl only**: proposals skips a feed with one (accents ignored; warez and sponsored-post words added 2026-11-21) as a whole word in its title,
  or 3 or more in its subtitle, newest posts and their categories together (asked 2026-11-21; `filters._has_keyword`, after the English filter). Agreed with the user on 2026-11-16 after porn in a proposals PR;
  never applied to the curated lists, submissions, load or export.
- `data/submissions.txt`: user-submitted feeds via PR, last source in sources.toml, read via `files`; tested.
- Common Crawl rows: source = WAT file URL, license `LicenseRef-CommonCrawl-ToU` (ranked last). No download
  retries: if the slice fails to download (e.g. a 503), the run fails; next week's run tries again. Feed health checks never retry.
- **Future item dates are ignored, not punished** (agreed 2026-11-28: 36 of 35,295 listed feeds had a newest post after
  today, pinned posts dated 2099/9999, theme defaults, typos, mostly good personal blogs): the parser drops item dates
  later than now + `feed.FUTURE_MARGIN` (1 day) for `last_item_date` and each recent post's date (an item with only such
  dates is undated), so the feed is sorted and kept listed by its real posts; all dates in the future gives no date (not
  listed, like an undated feed). No penalty otherwise; proposals show `future_dated`.
- Batches: the `--batch` most outdated inputs, never checked first, then oldest `last_checked_at` (ties random);
  none checked within `--check-cooldown-minutes` (1440, agreed 2026-11-22); storing a
  row sets `inputs.last_checked_at` whatever the outcome.
- Export: success in the last 180 days and an item in the last year (5 until 2026-11-18, changed with the user to match the site text); every topic with a feed gets a page (`--top-topics` caps it, default none: the 2000 cap was removed 2026-11-19 at the user's request; `feeds.topics`; agreed 2026-11-18:
  everything published is about topics, no tags; languages only in previews);
  per topic links.txt ≤ 50,000, feeds.opml ≤ 500, topic page ≤ 200 (most recently active; each file starts with the next one's feeds; 200 agreed 2026-11-17 for page size); index sorted by count.
  `all/{links.txt,feeds.opml}`: every listed feed, with a topic or not, uncapped.
  links.txt files are newest post first (a search page shows them in file order).
  `previews/<2 hex>/<sha256 hex of the URL>.json` per listed feed (for a search page, e.g. feedrule.com): `{url, title, subtitle, site, bytes, last, languages, topics, source, posts_url}` (site: the feed's website, http(s) only, asked 2026-11-28; posts_url: the feed's file on the `posts` branch, `--posts-url` + `export.posts_path`, null if none),
  bytes = body size at the latest successful check, languages = the detected labels (list), topics = the feed's topics (list, most similar first),
  source = `{name, license}`: the `site.SOURCES` entry inputs.source belongs to (`site.source_of`), else
  `{null, source_license}`; null if not in inputs (no URL, by the user's choice). Hash, not hex of the URL (file name limit).
  `SPEC.md` at the repo root (hand-written: the output's file patterns and contents) is copied into the output by export
  (`--spec`, mounted read-only in compose); keep it in sync with the export (a test checks patterns and limits).
  **No Markdown in the output (except SPEC.md): the website is HTML** in `output/docs/` (`site/`), served by GitHub Pages
  from the output branch's `/docs` (agreed 2026-11-17: pages that rank, links to feedrule.com without GitHub's nofollow,
  github.io keeps any thin-content risk off feedrule.com). Index (the 50 largest topics, a search box over all of them from an inline JSON list,
  no links to the all/ files (too big), sources table `site.SOURCES`: update it with data/sources.toml) and one page per topic (200 rows = the first of feeds.opml; under `site.MIN_INDEXED_FEEDS` (3) feeds: `noindex` and not in the sitemap, agreed 2026-11-19 against thin content); sitemap.xml, .nojekyll, canonical
  URLs (`--site-url`). Data files stay outside docs/ and are linked on raw.githubusercontent.com (`--data-url`), so they
  don't count toward the Pages limit. Colours: feedrule.com's (Tailwind zinc + orange), light and dark. Jinja templates
  (`src/indexer/publish/templates/`: base, index, topic, sitemap) with autoescaping on and StrictUndefined; `publish/site.py` only prepares
  the data. No front-end framework, no build step; small inline scripts (topic filter; "Show more" on topic pages: 20 rows, then 20 more per click,
  `site.SHOWN_ROWS`; the preview page). Pages may need JavaScript (the no-script rule was dropped 2026-11-28 at the user's request).
  **Preview page** (asked 2026-11-28): each row's meta line ends "Website · Feed · Preview"; Preview links
  `docs/preview/?sha=<SHA-256 hex of the feed URL>` (`site.url_hash`), one static page whose script accepts only
  `^[0-9a-f]{64}$`, fetches `previews/<2>/<sha>.json` (`--data-url`) and `data/posts/<2>/<sha>.json` (`--posts-url`) in
  parallel and builds the page with textContent only (never innerHTML), links only http(s) URLs (feed, site and post
  links nofollow); post descriptions cut at `site.EXCERPT_CHARS` (300); a missing preview says the feed is not listed,
  missing posts say none saved yet; an "Imported from <source name> (<license>)" line, the name linking the index's `#sources` table (previews keep no source URL); `noindex`, not in the sitemap.
  The stylesheet is linked as `style.css?v=<content hash>` (`site.STYLE_VERSION`: Pages caches it 10 minutes). The header links
  the GitHub repository ("Open source").
  Each feed row has feedrule.com's icon at its top right (last in the cell, labelled and titled "Email digest by Feedrule"; tried as a text line first) (`src/indexer/publish/feedrule.svg`, copied to docs/;
  moved there from before the title on 2026-11-28 at the user's request: a leading icon read as the feed's own logo or owner) linking to
  `https://feedrule.com/?feed=<feed URL, percent-encoded except : and />` (`site.feedrule_link`). Untrusted text is
  HTML-escaped; only http(s) URLs become links (`site.safe_href`); feed and site links are rel="nofollow", feedrule's not.
  Row: title and a "Website" link (after the plain-text domain) go to the website (the feed's `site_url`, else its domain's home page), the subtitle under the title (one line,
  ≤ 200 chars, not when it repeats the title; `site.subtitle_line`), then a "Feed" link,
  then the feed's other topics (`site.other_topics`), most similar first; published ones link to their page.
  `topic/topics.txt`: the published topic names, one per line, most feeds first (ties by name). Topic directories and
  URLs use `export.topic_slug` (+ → plus, # → sharp: `c++` → `cplusplus`); pages show the name.
  publish.yml force-pushes output/ as one orphan commit to the `output` branch (no history, by design).
- **Posts branch** (asked 2026-11-27: keep each feed's recent posts in git as long as the feed exists, beyond the
  database's 3 days): orphan branch `posts`, one file per feed at `data/posts/<2 hex>/<sha256 of the URL>.json` (the preview's name
  under data/posts/, asked 2026-11-27), `{url, posts}` (posts as in `recent_posts`: link (http(s) only), title, description, date (added 2026-11-28 for the preview page); no check date, asked 2026-11-27: keep it simple, so
  a recheck with the same posts writes the same bytes and git sees no change). `indexer export --posts-dir DIR` writes the
  file of every feed with posts in the database and never deletes one. publish.yml checks the branch out, moves
  its `.git` aside before the container runs (the container never sees .git or a token), commits what changed and pushes
  normally (**never force-pushed**: files are not re-uploaded), before force-pushing output; publish has its own concurrency
  group (queued, not cancelled). Posts are overwritten, not merged across checks (for now). clusters reads this branch.
- **Sentence clusters** (agreed 2026-11-22): 500 k-means clusters of MiniLM vectors of the sentences (≥ 3 words) of recent post
  titles and descriptions: the posts branch's files (the output previews until 2026-11-27) plus the rejected proposals on the proposals branch (not `reason: unsafe`,
  not with an excluded keyword). Written to `data/clusters/` (git-ignored on develop) and pushed by clusters.yml to the `binaries`
  branch: generated binaries off develop, paths mirroring the repo's, no history (each workflow replaces only its own folder, keeps
  the rest, force-pushes one commit); the push runs on the runner, the container has no token. Planned: the user marks unwanted
  clusters (in develop, e.g. `data/clusters/unwanted.txt`) to tell spam assessment what to ignore; count feeds, not just sentences.
- Cleanup keeps each URL's most recent successful check (whatever its age) plus 8 months of failed checks (refuses < 7), and clears
  `health_checks.recent_posts` (JSONB, the 3 most recent posts) on checks older than 3 days (agreed 2026-11-22). feeds and inputs are kept.
  It then reports the DB size; over 300 MB cleanup.yml opens an issue "Database over 300 MB" (if none with that title is open)
  with a simple report (table sizes, recent posts), tagging the user; the issue is created on the runner, never in the container.
- Git: commit each verified step to `develop`; push only when asked. There is no `main` branch yet.

## Stack and conventions

- Source in `src/indexer/`: `errors.py`, `sources.py` (sources.toml, list loading) at the top;
  `publish/` (`export.py`, the data files; `site.py`, the HTML site, with its Jinja templates, style.css and feedrule.svg),
  `text/` (models on plain text: no RSS, no database; `topics.py`, `TopicModel.topics(posts, title, subtitle)`; `language.py`,
  `LanguageModel.post_languages(texts)` / `.languages(texts)`; `embeddings.py`, MiniLM and the embedding sets; `clusters.py`, the sentence clusters),
  `cli/` (one module per command: `NAME`, `HELP`, `add_arguments(parser)`, `run(args)`, listed in `cli/__init__.py`'s `COMMANDS`; `common.py` for what several share; commands stay thin: parse, call a module, print), `net/` (guarded fetcher, URL validation, resolver, IP policy), `db/` (everything about the database: `database.py`, all SQL through psycopg; `sql/`, its statements, one per file, except the INSERTs built from the models; `liquibase/`, the changelog and Liquibase's Dockerfile, not Python; `models.py`, the tables as dataclasses: database.py builds column lists from them; a DB test checks them against the schema, so update them with every changeset), `check/` (runner, feed parser), `proposals/` (Common Crawl: `wat.py` only reads WAT files and imports nothing from indexer; proposals; `training.py`: the proposal filter).
- Python 3.13 with **uv** (commit `uv.lock`), ruff (lint + format), type hints everywhere, `mypy --strict src`.
- Tests use pytest with a local server, fake DNS and synthetic files. **Tests never touch the network.**
  Database tests run only when `INDEXER_TEST_DATABASE_URL` is set.
- Pin exact versions for images and actions (actions by commit SHA). This machine is aarch64: pick images that
  publish arm64. Liquibase 5 ships no JDBC drivers: `src/indexer/db/liquibase/liquibase.Dockerfile` adds the pinned, checksummed
  PostgreSQL driver; bump image, driver version and checksum together.
- Liquibase owns the schema: `src/indexer/db/liquibase/changelog/changes/NNN-*.yaml` (the changelog and its Dockerfile sit in the db package, outside the wheel and the app image), each listed in `db.changelog-master.yaml`.
  **Never edit an existing changeset; add a new one.** `001-schema` (author `feedrule-indexer`) squashes the former
  001–009; it was edited on 2026-11-17 (reset databases, at the user's request: author, MARK_RAN removed, dead columns dropped; on 2026-11-18 the former 002–005, languages and topics, folded in after another reset). Squash or edit
  only when asked; on a database that has data, a squash needs a new id, a MARK_RAN precondition and a schema dump diff.
- compose.yaml: the app services share an `x-indexer` anchor (image, DB settings, hardening: read-only
  root, no capabilities, no-new-privileges). DB settings default to the local postgres, whose password comes only from `POSTGRES_PASSWORD` in a git-ignored `.env`
  (`.env.example`; no password in the repo; `uv run` uses `PGPASSWORD`), and are overridden by env
  (`LIQUIBASE_COMMAND_URL/USERNAME/PASSWORD`, `DATABASE_URL`, `PGPASSWORD`). Workflows run
  `docker compose build` then `docker compose run --rm --no-deps -T liquibase|<service>`, with
  `contents: read` (publish: write; proposals: write; cleanup: `issues: write`), `persist-credentials: false` and one shared concurrency group (except publish: read-only on the database, its own group `publish` for the posts branch's pushes).
  Supabase secrets `SUPABASE_DB_*`; use the IPv4 session pooler (runners have no IPv6).
- All DB writes share one connection across concurrent checks; `Database` serializes them with a lock
  (concurrent transactions on one connection fail with OutOfOrderTransactionNesting).

## Security rules (all URLs are untrusted)

1. Only `http://` and `https://`. Reject URLs with credentials (`user:pass@`) or a port other than 80 or 443.
2. Connect only to public IPs. Reject the host if **any** address DNS returns is non-public: private, loopback,
   link-local (including 169.254.169.254), Azure's 168.63.129.16, CGNAT 100.64/10, multicast, reserved,
   unspecified, IPv6 ULA and link-local, and IPv4-mapped or NAT64 forms of these. IP literals get the same check.
   **The check must run on the exact address the socket connects to**: a guarded resolver inside the aiohttp
   connector with the DNS cache off, plus a socket-factory check. Never resolve, check, and then let a client
   resolve again (DNS rebinding / TOCTOU). Applies to every redirect hop.
3. At most 3 redirects, followed by hand; rules 1 and 2 on every hop; relative `Location` resolved; https→http
   allowed.
4. Limits: 7 s deadline per fetch (redirects included), 3 s connect timeout, 5 MB body cap **after decompression**
   (gzip bombs fail), header size cap, GET only, no cookies, proxy env vars ignored, honest User-Agent.
5. Parse XML with defusedxml (no DTD loading, no entity expansion). Ignore Content-Type.
6. Concurrency: 20 fetches in total by default, 50 in check.yml (`--concurrency`), at most 2 per host.
7. A failure never stops the run. Every URL gets exactly one row, with an error identifier if it failed.
8. Text from feeds is untrusted wherever it is published: escape it (Markdown, XML); URLs found in content are
   stored, never fetched.

## Schema (details in DEVELOP.md)

- `inputs`: url PK, source, source_license, first_seen_at, last_checked_at.
- `runs`: id, started_at, finished_at.
- `health_checks`: per-check data only (status, timings, sizes, item count, last item date, redirects, error,
  ip_address, etag/last_modified, recent_posts for 3 days). Keep rows small (~330 bytes with indexes). Unique (url, run_id).
- `feeds`: latest metadata per URL, updated in place (replaced on a parsed check, kept on failure). Descriptive fields use Atom names.
  `language`: the feed's labels, detected per item (agreed 2026-11-18; what the feed declares is not read): the categories the feeds publish are **not
  extracted any more**; each of the first 30 items' text (title + start of the description, not stored) goes through
  fastText lid.176 (`text/language.py`, model pinned by checksum in the Dockerfile, CC BY-SA 3.0); a language
  becomes its ISO 639 code when it is on ≥ 2 items and ≥ 20% of the items with ≥ 40 characters of text. Several
  languages give several labels, sorted, comma separated.
  `topics` (agreed 2026-11-18): the text (title + start of the description) of the items detected as `en`, plus the feed's own
  title and subtitle counted like 3 items (`topics.about_text`: boilerplate removed; only if the subtitle has ≥ 3 words
  of its own, not just the name), is embedded with MiniLM (all-MiniLM-L6-v2,
  quantized ONNX, Apache-2.0; model and tokenizer pinned by revision and SHA-256 in the Dockerfile), averaged, and
  compared with `data/embeddings/topics.npy` (the embedding set `topics`, of `data/topics/topics.txt`, 6,079 names used by ≥ 5 listed feeds);
  names mapped through `data/topics/synonyms.tsv` (at the check, and again by export on the stored topics); a feed gets topics only if its best name beats its 8th best (distinct)
  by ≥ 0.08, then up to 5 within 0.02 of the best and ≥ 0.35, most similar first (`text/topics.py`; tuned 2026-11-18
  on 55 random feeds judged by hand: title + description beat titles alone; **precision first** (user's choice):
  ~77% of the model's topics right, on ~half the feeds; 0.04 gave ~65% on ~80%; a fixed threshold did worse). Authors' tags are evidence only, **never stored** (agreed 2026-11-18): the
  parser reads each item's categories in memory (`FeedInfo.item_tags`); a topic they name (spelling folds, singular/
  plural, synonyms) on ≥ 2 and ≥ 20% of the English items, ignoring items with > 10 tags, is added after the model's
  picks if the model scores it ≥ 0.35 (plausible), up to 5 in all. A fixed boost was tried and changed nothing:
  backed topics usually rank far down; the floor keeps bookkeeping tags (journal, personal, essay) out.
  **Fallback (user's choice, 2026-11-18)**: a feed that gets nothing from the above takes the first label of its newest
  labelled English item if it names a topic (`topics.first_author_topic`; no other label tried, no plausibility floor),
  only when `en` is among the feed's detected languages (`feeds.language`, our fastText labels; nothing the feed declares).
  Measured on 1,000 feeds: 59 of 403 topic-less English feeds get one, ~40-45% right; accepted because a wrong label
  is at least the author's own.
  Names about blogging rather than a subject were removed from topics.txt (journal, personal, opinion, feeds, diary,
  hobbies, boredom, writing-life, blogging-about-blogging, end-of-year, blogger…) and names that matched everything
  in a 1,000-feed run (social-software, webtech, brainstorming, bazaar, projects, future): precision first.
  Embedding sets (asked 2026-11-21: one layout for all): `data/embeddings/<name>.npy` (float16, unit rows) + `<name>.json`
  (rows, dim, model, SHA-256 of model, tokenizer and each source by repo-relative path); `embeddings.read_set` refuses a stale
  set, `embeddings.write_set` builds one; MiniLM loading is in `text/embeddings.py`. `indexer embeddings [NAME ...]` rebuilds sets
  listed in `cli.EMBEDDING_SETS` (only `topics` so far, via `topics.build_index`); never edit a set by hand; embeddings.yml
  does it and commits `data/embeddings/` to develop (its own concurrency group: no database; network off in the container).
  No English item, no topics. `indexer proposals` does not compute topics.
- Proposal filter (agreed 2026-11-21 after testing MiniLM centroids/vectors, char n-grams, a pretrained quality model;
  this is the agreed quality heuristic for proposals): `indexer proposals` fits TF-IDF (words + word pairs) and a numpy
  logistic regression over accepted / rejected on the reviewed proposal files in `data/proposals/training.toml` (proposals branch)
  (today PR #18's and #19's, the only ones made with the current rules and format); label = `accept: true` or false (binary
  agreed 2026-11-22: same AUC as one class per reason, no reason labelling needed; a `reason: <reason>` field is an
  optional, unused note; feeds with `learn: false` are skipped, `training.read_examples`), text = what the reviewer saw (title, about, categories, posts' titles and descriptions). Trained by `indexer proposals` (up to a minute,
  deterministic) and kept in git-ignored `data/models/proposals-<hash of training.toml, the listed files and proposals/training.py>.npz`:
  reused while present, retrained when the hash changes; never committed (agreed 2026-11-22: no model binaries in git
  history). `indexer train` prints its size and cut-off.
  `indexer proposals` drops proposals scoring below the cut-off (the 30th percentile of the examples' cross-validated scores,
  `training.CUT`), only counting them in the summary, and writes the rest best first with `# score:`. Never run MiniLM,
  Docker builds or test suites on the user's Pi without asking (undervoltage shut it down).
- `error` identifiers live in `src/indexer/errors.py` (e.g. `dns_nxdomain`, `rate_limited`, `not_a_feed`).

## Commands

- `docker compose up -d postgres && docker compose run --rm liquibase`
- `uv run pytest`, `uv run ruff format . && uv run ruff check .`, `uv run mypy --strict src`
- `docker compose build` after code changes, before running services.

## Required tests

Each blocked IP range; the DNS rebinding case (public IP first, private IP on the next call, and no connection
is ever made to the private IP); too many redirects; a redirect to a private IP; an oversized body; a gzip bomb;
the scheme, port and credentials checks.
