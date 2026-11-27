# Development

How indexer works, how to run it, and how it is deployed. What the lists are, which feeds get
listed, where they come from, and when each workflow runs are in the [README](README.md); this
file doesn't repeat them.

## Commands

| stage | command | compose service | workflow |
| --- | --- | --- | --- |
| load feed URLs from the sources | `indexer load-inputs data/sources.toml` | `input` | `input.yml` |
| propose old Common Crawl feeds still active | `indexer proposals` | `proposals` | `proposals.yml` |
| check the most outdated inputs | `indexer run --from-inputs` | `check` | `check.yml` |
| write the published lists | `indexer export` | `export` | `publish.yml` |
| delete old checks | `indexer cleanup` | `cleanup` | `cleanup.yml` |
| delete the URLs in `data/undo.txt` | `indexer undo data/undo.txt` | `undo` | `undo.yml` |
| rebuild the topic matrix | `indexer embeddings` | `embeddings` | `embeddings.yml` |
| cluster the sentences of recent posts | `indexer clusters --posts DIR` | `clusters` | `clusters.yml` |
| **delete every table and all data** | `indexer drop-tables --yes` | `drop-tables` | `drop-tables.yml` (manual) |

All state is in Postgres: locally the compose `postgres` service, in deployment Supabase. The
schema is managed by Liquibase (`src/indexer/db/liquibase/changelog`).

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13) and Docker with
Compose v2.

```sh
cp .env.example .env                     # then set POSTGRES_PASSWORD in it (git-ignored)
export PGPASSWORD="$(sed -n 's/^POSTGRES_PASSWORD=//p' .env)"   # for uv run
docker compose up -d postgres            # Postgres on 127.0.0.1:5432 (user and db: indexer)
docker compose run --rm liquibase        # apply the schema
uv sync

uv run indexer run data/urls.txt -v    # check 100 sample feeds; -v lists each failure and why
uv run indexer load-inputs data/sources.toml
uv run indexer run --from-inputs --batch 50
uv run indexer export                  # writes ./output
```

Every command also runs in a container (`docker compose run --rm <service> [args]`), which is
how the workflows run them; rebuild after code changes with `docker compose build`. The
containers run as a non-root user with a read-only filesystem, no Linux capabilities and
`no-new-privileges`, and the address rules apply inside them too: `http://postgres/` resolves to
the database container's private address and is refused like any other non-public host.

Look at the results of the latest run:

```sh
docker compose exec postgres psql -U indexer -c \
  "select coalesce(error, 'ok') as outcome, count(*) from health_checks
   where run_id = (select max(id) from runs) group by 1 order by 2 desc"
```

Stop with `docker compose down` (add `-v` to delete the data volume). Point any command at
another database with `DATABASE_URL` or `--database-url`, a URL or a libpq keyword string
(`host=... dbname=...`); the password may come from `PGPASSWORD`.

## Inputs

The `inputs` table holds every feed URL to check, one row per URL. It only grows: nothing ever
deletes from it, so a broken or truncated upstream list adds nothing harmful and removes
nothing.

| column | meaning |
| --- | --- |
| `url` | the feed URL, in the form the checker validates (primary key) |
| `source` | where it was found: the list file's URL, or the Common Crawl file |
| `source_license` | that list's license (an SPDX id) |
| `first_seen_at` | when the URL was first loaded |
| `last_checked_at` | when it was last checked; `null` means never |

### Curated lists: data/sources.toml

`data/sources.toml` names the lists, each with its license and the URL(s) of its list file(s):

```toml
[[source]]
name = "kagi-smallweb"
license = "MIT"
format = "lines"                    # or "opml", or "proposals" (the proposal YAML files)
homepage = "https://github.com/kagisearch/smallweb"
urls = ["https://raw.githubusercontent.com/kagisearch/smallweb/main/smallweb.txt"]
```

The URLs point at a branch, not a commit, so each load reads the lists as they are today and
new feeds arrive without anyone editing the config. A source can also use `files`, paths in this
repository relative to `data/sources.toml`, read from disk. Files are downloaded with the
guarded fetcher; OPML is parsed with defusedxml, falling back to reading the `xmlUrl` attributes
when a hand-edited file is not well-formed (common). URLs are validated with the checker's
scheme, credential and port rules.

- **Licenses.** A URL found in several lists is credited to the most permissive one, by the
  `[licenses] ranking` (on a tie, the source listed first). Re-loading only moves a URL to a
  strictly more permissive source. Only openly licensed lists are used.
- **Exclusions.** Feeds on the registrable domains in `[exclude] domains` (and their subdomains)
  are never loaded.
- **Failures.** A source that fails (file gone, malformed) is skipped and reported, and the
  others still load. In GitHub Actions a skipped source is a warning annotation on the run; the
  command fails only if no source could be read.

The sources and their licenses are listed on the [site's home page](https://feedrule.github.io/indexer/#sources) (`site.SOURCES`).

### Submissions

`data/submissions.txt` is an ordinary source (`files = ["submissions.txt"]`), listed last in
`data/sources.toml` so a feed that is also in another list keeps that list as its source.
`uv run pytest` checks that every line of it (and every accepted URL in `data/survivors/`) is a
valid, unique URL.

### Proposals: feeds that survived from an old crawl

`indexer proposals` looks for feeds that have lasted, and **proposes** them for review rather
than adding them. It picks the crawl that ended closest to `--years-ago` (5) years ago and
downloads the first `--slice-mb` (25) MB of one random WAT file with a range request,
dropping the record cut off at the end. [Common Crawl](https://commoncrawl.org)'s WAT files record
the `<link>` elements of every crawled page; `indexer proposals` reads the feeds each page advertises with
`<link rel="alternate">`, validates them like any input and applies `[exclude] domains`. It never
takes a WordPress category, tag or author feed (`/category/news/feed/`, a subset of the main feed)
or a comment feed (WordPress `/comments/feed/` and `?feed=comments-rss2`, Blogger
`/feeds/comments/` and `/feeds/<post id>/comments/`). Every `http://` link is tried as `https://`.
A link must also **look like a feed URL** (`feed_links.py`): at most 256 characters, at most 7
slashes in the path and 25 characters in a path segment, none of the rejected patterns (a dated
path like `/2021/04/`, `comments`, `alt=rss`, ...), and what follows the host one of the accepted
shapes (`/feed/`,
`/rss.xml`, `/atom.php`, `/<one segment>/feed/`, ...). Forums and wikis are dropped too: "forum" or
"forums" as a host label or path segment (`forums.example.com`, `/forum/`, `-forums/`, but not
`hightechforum.org`), `feed.php` (DokuWiki, phpBB), `app.php/feed` (phpBB), `syndication.php`
(MyBB), `type=rss;action` (SMF), Invision Community's `/rss/public` and `/rss/<n>-…`, BuddyPress's
`/activity/feed` and Digital Commons'
`/recent.rss`; their feeds are mostly edits, replies, sign-ups and papers. These rules are applied
again to the URL a feed is served from after redirects (`unwanted_url`). So are feeds on
the top-level domains Freenom gave away (`.tk`, `.ml`, `.ga`, `.cf`, `.gq`), mostly spam.
WordPress's feed in another format (`/feed/atom/`, `/feed/rss2/`, `/feed/rss/`, `/feed/rdf/`) is
read as its main feed, `/feed/`, so a feed already known or proposed isn't proposed again in
another format. These
rules only look at the URL and live in `feed_links.py`; the rules below need the checked feed and
live in `filters.py`. Feeds outside those
shapes are dropped (FeedBurner among them), except the main feeds of platforms without a feed
word in the path: Blogger `/feeds/posts/default`, WordPress without pretty links `/?feed=rss2`,
Joomla `?format=feed&type=rss`, SPIP `spip.php?page=backend` and `backend.php`. Google Web
Stories feeds (`/web-stories/`) are dropped, and so is the page's own feed (the page URL followed
by `/feed`, a post's comments) when the page offers another. Session parameters (`sid`,
`PHPSESSID`, `jsessionid`, ...) are removed. It checks **every** remaining link, each URL once and at most 10 per site (the shortest
URLs first), today with the guarded fetcher (no retries), and of the feeds
that pass on one site it keeps the one with the shortest URL (ties by name), so one site is proposed
once. A site is the host without `www.` (not the registrable domain, which would merge the users of
shared hosts like `fc2.com` or `free.fr`). A feed is proposed only if:

- it parses, and its newest item is from the last `--active-days` (243, about 8 months) days (a
  date more than a day in the future doesn't count);
- it is **in English**: `en` is the only language detected on its items (see `language`
  below). The feed's own `<language>` is not read: WordPress says `en-US` unless the owner changes it;
- it **has a title**, and at least one of its recent posts has a title or text (a missing
  description is fine: removed on 2026-11-21, it would have dropped 1 in 7 of the listed feeds);
- neither its title nor its description names a business or an organisation ("service(s)",
  "attorney(s)", "agency", "book now", "supplies", "equipment", "shop", "company", "store",
  "consultants", "law firm", "law group", "LLC", "radio station", "station for",
  "congregation", "parish": `filters._BUSINESS_WORDS`), and its description doesn't start
  "Jobs from" (job boards). About 1 in 140 of the listed feeds would be dropped by them, mostly
  consultants' and agencies' good blogs;
- its title contains none of the `[commoncrawl] exclude_keywords` in `data/sources.toml`
  as a whole word, and its subtitle, newest posts (title and start of each) and the titles of
  its 3 most recent posts, and the categories of its posts (once per post), fewer than 3
  (every occurrence counts; accents are ignored, so "casino" matches "Casinò").
  This list is for Common Crawl only: the curated lists and submissions are not filtered by it
  (tried on their 9,048 feeds, it would flag 14: 3 adult, the rest blogs like "Budgets Are Sexy");
- none of its 3 most recent posts links to a wiki edit (`do=diff`, `rev=`, `oldid=`, `doku.php`,
  `index.php?title=`) or has `utm_` tracking parameters in its link, and no post says "appeared
  first on" or "first appeared on" (the footer of SEO plugins, common on promotional sites and
  on sites that copy other feeds), and none is WordPress's untouched sample post ("Welcome to
  WordPress. This is your first post": an abandoned or hijacked install), and no two of them have
  the same title and text (the same post published again), and no two of their descriptions
  start like an SEO meta description ("Discover", "Learn", "Explore", "Understand", "Uncover",
  "Find out": content farms), no two of their descriptions start with their own title
  (generated text), and no post says "article appeared in" (a repost from another outlet).
  These simple rules also drop some good feeds, which is fine:
  there is far more in Common Crawl than one run reads;
- it is **served over https today**: the URL after redirects is `https://` (links are tried as
  `https://`; one that redirects back to plain http is skipped);
- it is not already in a file in `data/survivors/` (accepted or not) or the inputs table.

Feeds already there are skipped before checking (and again after, for the URL a redirect led
to). The rest are checked in random order, 200 at a time, and the run stops once `--max-feeds`
(20) pass, so a run proposes at most 20 and stops checking early.

The finds go to a new file per run, `data/survivors/<yyyymm>/<dd-HHMMSS>.yaml` in UTC (for example
`data/survivors/202611/19-041105.yaml`), which the source `survivors` reads with
`format = "proposals"` and the glob `files = ["survivors/*/*.yaml"]`. The file is YAML: a comment
header, then one document per feed, separated by `---`: `url`, `accept: false`, `title`,
`language`, `last_post` (the newest item's date), `about` (the subtitle), `posts_read` and
`categories` (the 8 categories its authors used most on those posts, with counts), `found_in`
(the WAT file) and `recent_posts` (the 3 most recent: `link`, `title` and `description`, the
start of the description as lines of at most 60 characters, for phones). Untrusted text is
flattened to one line and written by `yaml.safe_dump`, which quotes anything YAML would read as
something else; files are read with `yaml.safe_load_all`.
Each workflow run commits
that file to a **new branch** with the same timestamp (for example `proposals-20261119-041105`),
and opens **its own pull request** into `develop`, whose description links to the file in
GitHub's editor and gives the run's summary; the feeds themselves are only in the file, so no
untrusted text reaches the pull request. The run also writes the proposed feeds as OPML
next to the `.yaml` file (same name, `.opml`; not read as a source), and the description links to
it: download it and import it into a feed reader to try them while reviewing. Review the diff, set **`accept: true`** on each feed you want (optionally
`reason: <reason>` on a rejected one, see below), and merge (or close the pull request to drop the whole batch): the input job then
loads the accepted feeds on the push to `develop` (source `survivors`, license
`LicenseRef-CommonCrawl-ToU`, ranked last). Every run starts from `develop`, so a pull request never carries an older run's proposals.
Each pull request adds its own file, so open pull requests never conflict and can be merged in any
order. URLs in another open pull request are not skipped, so the same feed can be proposed twice. A feed left at `accept: false`
counts as known (`files.known_urls` reads every file in the folder), so it is not proposed
again; one deleted from its file is forgotten.

Started by hand (Actions → proposals → Run workflow), the workflow takes `slice-mb`, `years-ago`
`active-days` and `max-feeds` as inputs; the weekly run uses the defaults (25, 5, 243, 20).

The workflow needs the repository setting **Settings → Actions → General → "Allow GitHub Actions
to create and approve pull requests"**. Titles are untrusted: in the file they are
flattened to one line, and in the pull request they are shown as code, so they can't add URLs,
mention people or link issues.

```sh
mkdir -p output && HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose run --rm proposals \
  proposals --slice-mb 5 --report output/proposals.md
```

In the pilot, a 20 MB slice of the 2021-49 crawl held about 3,200 pages and 2,300 feed URLs from
900 sites. 216 had posted within a year; 56 (38 sites) also had tags and were served over https,
and 14 of those (8 sites) were labelled English. With the default-tag, category-feed, session and
format rules that becomes 10 (6 sites). The first real run (5 slices of 20 MB, CC-MAIN-2021-43)
checked 11,944 feed URLs: 4,685 worked, 612 had a recent tagged item, 209 were labelled English
and 104 were on https (0.87%). A run reads one 25 MB slice: about 3,000 feed URLs to check. One file
gives as many distinct feeds per MB as several smaller slices (about 125 per MB in samples of 5 to
30 MB from one file and of 5 × 20 MB), so there is no reason to spread the sample. The checks made
here are not stored: once merged, the inputs get their regular checks from `check`. If the slice
fails to download (Common Crawl answers busy periods with 503), the run fails without retrying;
the next weekly run picks another file.

## Checking

```sh
uv run indexer run FILE                     # every URL in a text file
uv run indexer run --from-inputs            # the 500 most outdated inputs
uv run indexer run --from-inputs --batch 50 --check-cooldown-minutes 60 -v
```

A URL file has one URL per line; blank lines and `#` comments are ignored, and duplicates are
checked once. Either way the URLs are checked in random order. `data/urls.txt` is a 100-feed
sample of
[simevidas/web-dev-feeds](https://github.com/simevidas/web-dev-feeds) (MIT) for quick runs.

`--from-inputs` checks the `--batch` most outdated inputs: those never checked first, then the
longest since their last check (ties in random order, so a batch doesn't bunch up on one host).
An input checked less than `--check-cooldown-minutes` ago (1440: a day) is never picked.
Storing a result sets `last_checked_at` whatever the outcome, so repeated runs work through every
URL before revisiting any: at 8 runs a day of 500, a full pass over ~47,000 curated inputs takes
about 12 days, and a new input is checked in the next run.

Each result is written as soon as its check finishes, so an interrupted run keeps what it has
done (its `finished_at` stays empty). `-v` prints each failed URL with the reason; successes only
appear in the summary. Other options: `--concurrency` (20; check.yml passes 50), `--per-host` (2), `--deadline`
(7 s). The `ETag` and `Last-Modified` of each successful response are stored as sent (values
that are not printable ASCII, or longer than 1024 characters, are dropped); nothing sends them
back yet.

## Published lists

`indexer export` writes the data files: `output/topic/<slug>/` (`feeds.opml`, `links.txt`),
`output/topic/topics.txt` (topic names, most feeds first), `output/all/` (`links.txt`, `feeds.opml`:
every listed feed) and `output/previews/` (one JSON file per listed feed, for a client-side search
page: fetch a topic's `links.txt`, then the previews of the results shown; see
`export.preview_path` and `export.render_preview`). `links.txt` files are most recently active
first. The feeds' recent posts are not in the output: with `--posts-dir` (a checkout of the
`posts` branch) export writes one file per feed there (`export.posts_path`,
`export.render_posts`: the feed URL and the posts; the same posts give the same bytes, so git sees
no change) and never deletes one, so a feed keeps its latest
posts after cleanup clears them from the database. Each preview's `posts_url` links to that file
(`--posts-url`, default `https://raw.githubusercontent.com/feedrule/indexer/posts/`), or is null. It also writes the website, `output/docs/` (`src/indexer/publish/site.py`): `index.html` (the 50 largest
topics, a search box over all of them, and the sources),
`topic/<slug>/index.html` for every topic (the 200 most recently active feeds, each with a
feedrule.com link prefilled with the feed and its other topics), `style.css` and `feedrule.svg` (package data:
`src/indexer/publish/style.css`, `src/indexer/publish/feedrule.svg`), `sitemap.xml` and `.nojekyll`. Topic pages with fewer than 3 feeds are marked `noindex` and left out of
the sitemap (near-empty pages would count as thin content); they are still linked and searchable. Pages link
`style.css?v=<hash of its content>`, so browsers load a changed stylesheet at once. GitHub Pages
serves the `output` branch's `/docs` folder; the pages link the data files on
raw.githubusercontent.com, so they don't count toward the Pages size limit. `--site-url` (default
`https://feedrule.github.io/indexer/`) is used for canonical links and the sitemap; `--data-url` for the data links. There is
no Markdown in the output except `SPEC.md`.

The files and listing rules are described in the [README](README.md#which-feeds-are-listed). The
limits are options: `--top-topics` (default: all), `--success-days`, `--item-years`. A topic's `<slug>` is its name with `+`
spelled `plus` and `#` spelled `sharp` (`export.topic_slug`). Titles and
URLs come from the feeds: the pages are Jinja templates (`src/indexer/publish/templates/`) with
autoescaping on, the OPML is escaped by hand, and only http(s) URLs become
links (a site URL could be `javascript:`); feed and site links are `rel="nofollow"`. `SPEC.md`,
the hand-written description of these files at the repository root, is copied into the output
(`--spec`; the compose `export` service mounts it read-only). The output directory is cleared on
every run, but only if it is empty or holds the `.indexer-output` marker, so a wrong path cannot
wipe anything else.

```sh
mkdir -p output posts && HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose run --rm export
```

`publish.yml` force-pushes `output/` as a single fresh commit to the `output` branch, which never
accumulates history. It runs right after each successful check run (`workflow_run`, which uses the
file as it is on the default branch), on a push to develop that changes `src/indexer/publish/`, on
Mondays as a fallback, and when started by hand (Actions → publish → Run workflow). It only reads the database.
Before that it checks out the `posts` branch, moves its `.git` aside so the container only gets
the files, and after the export commits what changed and pushes it normally (never forced), before
the output, so a preview never links a missing file. Runs queue in one concurrency group, so two
pushes never race.

## Cleanup

`indexer cleanup` keeps only each URL's most recent successful check (however old) and the failed
checks of the last 8 months (`--months`); it deletes the rest, and runs older than that with no
checks left. It then clears `recent_posts` on checks older than 3 days (`--posts-days`): they are
most of a check's size. `feeds` (each feed's latest metadata) and `inputs` are kept. It refuses
fewer than 7 months.

It then prints the database's size and what takes it (each table, indexes included, and the recent
posts). Over `--report-over-mb` (300) it prints a `::warning::`, and with `--report FILE` writes
that report to FILE: cleanup.yml then opens an issue titled "Database over 300 MB" with it, tagging
the maintainer, unless an issue with that title is already open. The issue is opened by a step on
the runner; the cleanup container never holds the GitHub token. Deleted space is reused by
Postgres rather than returned at once, so the size may only drop after autovacuum.

## Sentence clusters

`indexer clusters` groups the sentences of recent posts into 500 clusters, unsupervised: the post
titles and descriptions of the posts branch's files (written by `indexer export`) and of the
rejected proposals in `data/survivors/` (not those rejected as `unsafe`
or with an excluded keyword, so none of that reaches the examples). Each distinct sentence of at
least 3 words is embedded once with MiniLM; seeded spherical k-means makes the same sentences give
the same clusters. It writes `data/clusters/`:

| file | contents |
| --- | --- |
| `posts.npy` | the 500 centres, one unit-length row each, float16 (like an embedding set) |
| `posts.json` | how they were built (model hashes, counts, the posts commit) and per cluster its sentences, feeds, rejected feeds and top words |
| `posts.md` | per cluster: counts, top words and the sentences closest to its centre (untrusted text, in code spans) |

These files are not committed to develop (`.gitignore`): `clusters.yml` (Mondays, or by hand)
checks out the posts branch into `published/`, runs the `clusters` service (no
network, no database, no token) and pushes the files to the `binaries` branch. That branch mirrors
the repository's paths and has no history: each run keeps its other files, replaces
`data/clusters/`, and force-pushes one new commit. To use them in a checkout of develop:

```sh
git fetch origin binaries && git checkout origin/binaries -- data/clusters
```

Clusters are counted in feeds as well as sentences: a cluster whose sentences come from one or two
feeds says little about other feeds.

## Undo: deleting listed URLs

`indexer undo data/undo.txt` deletes each URL in the file (one per line, `#` comments) from
`inputs`, `health_checks` and `feeds`, in one transaction. A URL matches as written and in the
canonical form the loader stores (e.g. `HTTPS://Example.com:443/feed` matches
`https://example.com/feed`). It prints how many rows it deleted from each table per URL, and
the URLs it found in no table. `undo.yml` runs it against Supabase on a push to `develop` that
changes `data/undo.txt` (or by hand) and copies the report to the job summary. Re-running is
harmless: deleted URLs are then reported as not found. A URL still listed in a source (a list in
`sources.toml`, a file in `data/survivors/`, `submissions.txt`) comes back with the next input load, so
remove it there as well.

## Starting over: drop-tables

`indexer drop-tables --yes` drops this project's tables (`inputs`, `feeds`, `health_checks`,
`runs`) and Liquibase's `databasechangelog` and `databasechangeloglock`, with all their data. It
drops only those names, in the connection's schema, so other tables in the database are left
alone; without `--yes` it refuses. The next migration (every workflow runs one first) recreates
the schema empty, and the input job reloads the URLs from `data/sources.toml`. The check history,
and any inputs that exist only in the database, are lost for good.

`drop-tables.yml` runs it against Supabase. It never runs on a schedule: start it from the
Actions tab and type `delete all tables` as the confirmation; anything else skips the job. It
waits for any running pipeline job (same concurrency group).

## Deployment: GitHub Actions and Supabase

The workflows in `.github/workflows` run everything through docker compose, exactly as
locally: build the images, migrate the schema with the `liquibase` service, then run the stage's
service, all with `docker compose run --no-deps` so the local `postgres` service never starts.
The database settings in `compose.yaml` default to the local database and are overridden from
the environment (`LIQUIBASE_COMMAND_URL`, `LIQUIBASE_COMMAND_USERNAME`,
`LIQUIBASE_COMMAND_PASSWORD`, `DATABASE_URL`, `PGPASSWORD`). The workflows share one concurrency
group, so they never overlap; they use a read-only repository token (except `publish.yml`, which pushes `output`, and
`proposals.yml`, which pushes `proposals-<timestamp>` and opens its pull request) and
pin the checkout action by commit SHA. They need these repository secrets:

| secret | value |
| --- | --- |
| `SUPABASE_DB_HOST` | the **session pooler** host, e.g. `aws-0-<region>.pooler.supabase.com`: GitHub-hosted runners have no IPv6, and Supabase's direct database host is IPv6-only |
| `SUPABASE_DB_PORT` | `5432` (session mode, not the 6543 transaction pooler) |
| `SUPABASE_DB_NAME` | `postgres` |
| `SUPABASE_DB_USER` | `postgres.<project-ref>` |
| `SUPABASE_DB_PASSWORD` | the database password |

To run the same steps from your machine against Supabase, put those values in a git-ignored
`supabase.env` and use `docker compose --env-file supabase.env run --rm --no-deps <service>`.

## Data model

`runs` has one row per `indexer run` (`id` is the run number, `started_at`, `finished_at`).

`health_checks` has one row per URL per run, with only per-check data (about 330 bytes per row, plus up to ~6 KB of recent posts for 3 days: 3 posts, descriptions up to 1,000 characters):

| column | meaning |
| --- | --- |
| `url` | the URL as given |
| `final_url` | the last URL requested (after redirects) |
| `http_status` | status of the last response |
| `parent_domain` | registrable domain of `url`, from the public suffix list bundled with `publicsuffixlist` |
| `redirected` | at least one redirect was followed |
| `first_redirect_status` | status of the first redirect: 301/308 permanent (update the URL), 302/303/307 temporary |
| `ip_address` | the final hop's server address; for a failed connection, the last address tried |
| `file_size` | bytes of the final body, after decompression |
| `item_count` | number of `<item>` / `<entry>` elements |
| `last_item_date` | newest published/updated date among the items |
| `ttl` | RSS `<ttl>` in minutes, if present and valid |
| `fetch_time_ms` | request start until the body is fully read, redirects included |
| `response_time_ms` | time to the response headers of the final request |
| `error` | `null` on success, otherwise one fixed identifier (below) |
| `etag`, `last_modified` | the validators of the final 200 response, as sent |
| `recent_posts` | JSON list of the feed's most recent posts (up to 3, newest first: `link`, `title`, `description`, untrusted text); `null` on failure, and cleared by `cleanup` after 3 days |

`feeds` has one row per feed URL with its **latest** metadata, updated in place: a check that
parses the feed replaces every column (a field the feed no longer has becomes `null`), a failed
check keeps the last known values.

| column | meaning |
| --- | --- |
| `url` | the feed URL (primary key) |
| `updated_at`, `parsed_at` | when a check last changed this row; when one last parsed the feed |
| `feed_kind` | `rss`, `rdf`, `atom` or `atom03` |
| `generator` | the feed's `<generator>` (or RDF `admin:generatorAgent`) |
| `site_url` | the website the feed names (RSS channel `<link>`, Atom alternate link) |
| `self_url` | where the feed says it lives (`atom:link rel="self"`) |
| `new_feed_url` | `<itunes:new-feed-url>`: the feed declares it has moved |
| `title` | feed title (Atom `<title>`, RSS channel `<title>`) |
| `subtitle` | Atom `<subtitle>` or RSS `<description>`, up to 500 characters |
| `language` | languages detected on the items: `de, en` (see below); what the feed declares (`<language>`, `xml:lang`) is not read |
| `topics` | topics from the text of the English items, most similar first: `rust, async` (see below) |
| `media_type` | `video` or `podcast` when at least half the items carry video or audio, otherwise `text`; `null` for an empty feed |
| `icon`, `logo` | Atom `<icon>`; Atom `<logo>`, RSS `<image><url>` or `<itunes:image>` |
| `rights`, `license` | Atom `<rights>`, RSS `<copyright>` or `dc:rights`; a license URL (`link rel="license"`, `creativeCommons:license`) |

The categories feeds publish are not read. `language` comes from `text/language.py` (plain text only, like `text/topics.py`): the text of
each of the first 30 items (title and the start of the description, markup stripped, up to 300
characters; kept in memory, never stored) goes through fastText's language identification model
`lid.176.ftz` (176 languages; the Docker image downloads it at a pinned SHA-256 and sets
`INDEXER_LID_MODEL`; model by Facebook AI Research, CC BY-SA 3.0). Items with under 40 characters
of text are skipped. A language becomes a label, an ISO 639 code, when it is detected on at
least 2 items and 20% of the judged items; labels are sorted and comma separated. Export
lists them in the previews; only English items are used for topics.

`topics` comes from `text/topics.py`, which works on plain text only (no RSS parsing, no language
detection, no database): the checker gives it the text of each item detected as English (its title and
the start of its description, as used for language detection) and the feed's own title and
subtitle (counted like 3 items; boilerplate such as "Just another WordPress site" or "Recent content
on …" removed; used only when the subtitle has at least 3 words of its own, since a bare name such
as "Feep! blog" dilutes the posts) are embedded with MiniLM (`all-MiniLM-L6-v2`, quantized ONNX, Apache-2.0; the image downloads the model and its
tokenizer at a pinned revision and SHA-256 and sets `INDEXER_MINILM_MODEL` and
`INDEXER_MINILM_TOKENIZER`), averaged into one vector and compared with every row of
`data/embeddings/topics.npy`, the precomputed embeddings of the names in `data/topics/topics.txt`
(one row per line, float16; the embedding set `topics`, see "Embedding sets" below). Names are mapped to their canonical form with `data/topics/synonyms.tsv`, at the
check and again by export (so a new synonym shows at the next publish, not only as feeds are re-checked). A feed
gets topics only if its best name beats its 8th best (distinct) name by at least 0.08: a vague
feed scores many unrelated names about equally. It then gets up to 5 names within 0.02 of the
best and at least 0.35, most similar first. Input and thresholds were tuned on 55 random listed
feeds, judged by hand: title + description found about 55% more right topics than titles alone;
wrong topics matter more than missing ones, so the thresholds favour precision: about 77% of the
model's topics were right, on about half of the feeds (a spread of 0.04 gave about 65% on about
80% of the feeds).
A fixed threshold did worse (43% right at 0.35 with titles; at 0.6, 68% right but on 17 of 58
feeds).

Authors' tags help as evidence and are never stored. The parser reads each item's categories
(RSS `<category>`, Atom `<category>`, `dc:subject`) in memory. A topic they name (matched after
dropping case, spaces, punctuation and a hashtag sign, in singular or plural, and through the
synonyms) on at least 2 English items and 20% of them is added after the model's own picks, up to 5
in all, if the model scores it at least 0.35 for the feed's posts. Items with more than 10 tags don't
count (tag stuffing). The floor keeps out tags about the blog itself (`journal`, `personal`,
`essay` score 0.15 to 0.32) and tags unrelated to the posts. On the 56 English feeds of the tuning
sample this added topics to 8 feeds (12 of 15 additions right, the rest close), and gave topics to
2 feeds the model alone left without.

A feed that still has no topic takes its author's first label: the first category of the newest
English item that has categories, if it names a topic (`first_author_topic`); no other category is
tried and the model doesn't vet it. This applies only when `en` is among the feed's detected
languages (`language`): a feed with a few English posts among others gets no fallback. On 1,000 sampled feeds this gave a topic to 59 of the 403
English feeds left without one; about 40-45% of those were right by hand judgment (filing labels
such as `news`, `archive` or `life` make up most of the rest), against about 75% for the model's
picks. It was accepted because a wrong topic is then at least one the author chose.

The feed's title and subtitle fix many feeds whose posts say little ("The Druids Garden. Spiritual
journeys in tending the living earth" gives `gardening, nature-writing, garden`) but pull towards
names about blogging when the subtitle is "Personal blog" or "Journal". Those names (`journal`,
`personal`, `opinion`, `feeds`, `diary`, `hobbies`, `writing-life`, `blogging-about-blogging`, …)
were removed from `topics.txt`, as were names that came out in the top 8 of many unrelated feeds
in a 1,000-feed run (`social-software`, `webtech`, `brainstorming`, `bazaar`, `projects`,
`future`). `indexer run` refuses to start if the topic set is stale; `indexer embeddings topics`
embeds every name in `topics.txt` in file order, with `-` and `_` read as spaces. To add or remove
a topic, edit `topics.txt` and let the workflow (or the command) rebuild the set.

**Embedding sets.** Every precomputed MiniLM embedding lives in `data/embeddings/`, one set per
name: `<name>.npy` (one unit-length row per text, float16) and `<name>.json` (rows, dimension, the
model, and the SHA-256 of the model, the tokenizer and each source file, by its path from the
repository root). Loading a set (`embeddings.read_set`) refuses it if any of those changed or the
row count differs, so a stale set is never used; `embeddings.write_set` builds one. `indexer
embeddings [NAME ...]` (compose service `embeddings`, no database, no network; about 20 s for
`topics`) rebuilds the named sets, or all of them; the sets it knows are in `cli.EMBEDDING_SETS`
(today only `topics`, from `data/topics/topics.txt`). The `embeddings.yml` workflow runs it on
every push to develop that changes a set's source or the `Dockerfile` (which pins the model)
and commits `data/embeddings/` to develop; it can also be started by hand. To add a set: a builder
that calls `write_set` with its texts and source files, an entry in `cli.EMBEDDING_SETS`, and its
sources in the workflow's `paths`. Never edit a set by hand. The quantized model's
output depends slightly on which names share a batch, so a rebuild also moves unchanged names a
little (cosine ~0.99 to their old vector); the same `topics.txt` always gives the same matrix. A feed with no
English item gets no topics.

**Proposal filter.** `data/training.toml` lists reviewed proposal files (paths relative to it).
Every feed in them is a labelled example: `accept: true` or `accept: false`. A
`reason: <reason>` on a rejected feed (one of
`training.REJECT_REASONS`: commercial, seo, press, republished, machine, forum, notices, unsafe,
other) is an optional note for people: it is checked but the model doesn't use it. Its text is what the reviewer saw (title,
about, categories, recent posts' titles and descriptions; not the language, dates, the WAT file,
links or a score), read from those fields. `indexer proposals` trains the classic cheap text classifier on
them (up to a minute) and keeps it in `data/models/proposals-<hash>.npz` (git-ignored; never
committed, so the repo doesn't grow with each retrain). The hash covers `training.toml`, every
listed file and `proposals/training.py`: a stored file is up to date by construction, so later runs load it
instead of training, and a changed review or code gets a new name and is trained once (older
files are removed; a directory it can't write to only costs the training time). TF-IDF over words and pairs
of neighbouring words, then a logistic regression over accepted and rejected, in numpy. Training
is deterministic: the same files always give the same model. The cut-off is the 30th percentile
(`training.CUT`) of the examples' cross-validated scores. `indexer train` trains it the same way
and prints its size and cut-off. Each new proposal's text (taken from its own rendered block) gets its probability of "accepted"; proposals
below the cut-off are dropped (only counted, in the summary), the rest are written best first with
`# score: 0.874` under the title (`--training`; with a warning and no filtering if it can't be
trained, e.g. the config is missing or lists no accepted or no rejected feed). List only files made with the
current proposal rules and format, so all examples look alike. Measured on 2026-11-21 (trained on PRs #18 and #19, 500 feeds; tested on
300 fresh proposals made with today's rules, labelled the same way): AUC 0.82 (MiniLM centroids
0.76, MiniLM vectors, character n-grams and a pretrained text-quality model did no better);
dropping the lowest 30% removed about half the rejects and 10% of the accepted feeds. On
2026-11-22 one class per reason was replaced by two (accepted, rejected): AUC 0.81, the same
within noise, and every plain review becomes training data without labelling reasons. The score
rose by about 0.025 per doubling of the examples, so it improves slowly as reviews are added.

To try the model on any text, without a feed or a database, each TEXT being one post:

```sh
docker compose run --rm --no-deps check topics --title "Food in Jars" \
  --subtitle "A blog about canning, pickling, and cooking from scratch" \
  "Salted Vanilla Fig Jam. A sweet and salty jam" "How to Can Frozen Fruit. ..."
```

`--tags A,B` (repeatable, in the order of the TEXTs) gives each post's own tags.
`indexer languages TEXT...` does the same for languages: each post's detected language, then the
feed's labels.

Text columns have
markup stripped (with the contents of `script`, `style`, `noscript` and `template`, which are
code, not text) and whitespace collapsed, and are capped at 200 characters unless noted. URL
columns come from untrusted content: resolved against `final_url`, kept only if they are absolute
`http(s)` URLs without control characters (up to 2048 characters), and never fetched.

Error identifiers (`src/indexer/errors.py`): `invalid_url`, `blocked_ip`, `dns_nxdomain` (the
name does not exist or has no addresses), `dns_temporary`, `dns_error`, `connect_error`,
`tls_error`, `timeout`, `too_many_redirects`, `bad_redirect`, `rate_limited` (429, or 503 with
`Retry-After`), `http_4xx`, `http_5xx`, `http_other`, `header_too_large`, `protocol_error`,
`too_large`, `unsupported_encoding`, `decode_error`, `not_a_feed`, `parse_error`,
`internal_error`.

## Security model

Every URL is untrusted. indexer:

- accepts only `http`/`https`, rejects credentials in the URL, and allows ports 80 and 443 only;
- connects only to public IP addresses. Private, loopback, link-local (including
  169.254.169.254), Azure's platform address 168.63.129.16 (reachable from GitHub-hosted
  runners), CGNAT, multicast, reserved, documentation, ULA and similar ranges are blocked,
  including their IPv4-mapped, NAT64 and 6to4 forms (`src/indexer/net/ippolicy.py`);
- is safe against DNS rebinding: the address check runs inside aiohttp's connector, on the exact
  addresses the socket then connects to (`GuardedResolver`, DNS cache off), and again in the
  socket factory right before each socket is created. There is no second lookup;
- follows at most 3 redirects by hand, re-applying every rule to each hop;
- enforces a 7 s deadline per fetch (redirects and body included), a 3 s connect timeout, a
  5 MB body limit counted after decompression (gzip bombs stop at the limit), header size/count
  limits, GET only, no cookies, and ignores proxy environment variables;
- parses XML with defusedxml (no entity expansion, no external references, DTDs never loaded)
  and ignores `Content-Type`;
- runs 20 fetches at once by default (50 in check.yml), at most 2 per host; a failure never stops the run.

Lists and Common Crawl files are downloaded through the same fetcher (with larger size limits).
Text from feeds is escaped wherever it is published. The User-Agent is
`indexer/<version> (awesome-rss feed health checker)`.

## Development

New to machine learning? ML.md covers the basics you need first.

Source layout (`src/indexer/`):

| path | contents |
| --- | --- |
| `cli/` | the `indexer` commands, one module each (`NAME`, `HELP`, `add_arguments`, `run`), listed in `__init__.py`; `common.py`: what several share |
| `db/database.py` | all database reads and writes (SQL through psycopg) |
| `db/sql/` | the SQL statements database.py runs, one per file |
| `db/models.py` | the tables as dataclasses, checked against the migrated schema by a test |
| `db/liquibase/` | the schema: Liquibase's changelog (`changelog/changes/NNN-*.yaml`) and its Dockerfile; not Python, left out of the wheel and the app image |
| `errors.py` | the error identifiers |
| `publish/` | the output: `export.py` (the published lists, data files) and `site.py` (the website in `output/docs/`: prepares the data for the Jinja templates in `templates/`; `style.css`, `feedrule.svg`) |
| `sources.py` | data/sources.toml and loading the lists into inputs |
| `net/` | fetching untrusted URLs: `fetch.py` (the guarded fetcher), `validate.py` (URL rules, URL files), `resolver.py`, `ippolicy.py`, `urls.py`, `domains.py` |
| `check/` | `runner.py` (one check per URL), `feed.py` (parsing RSS/Atom) |
| `proposals/` | `run.py` (the pipeline, steps 1–5), `wat.py` (reading Common Crawl: the `<link>`s of each HTML page in a WAT slice; imports nothing from indexer), `feed_links.py` (the URL rules before checking: `scan_wat`, `filter_page_links`), `filters.py` (the rules after checking), `files.py` (the data/survivors/ files, their OPML, the run summary and the PR report), `scoring.py` (dropping proposals the trained model scores low), `training.py` (training that model on reviewed proposals) |
| `text/` | models on plain text: `topics.py`, `language.py` (fastText), `embeddings.py` (MiniLM and the embedding sets in data/embeddings/) |

```sh
uv run pytest            # no network needed: local test server and fake DNS
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
```

The database tests run when `INDEXER_TEST_DATABASE_URL` is set, e.g.
`INDEXER_TEST_DATABASE_URL=postgresql://indexer@127.0.0.1:5432/indexer`, with the password in
`PGPASSWORD`.

The schema lives in `src/indexer/db/liquibase/changelog/changes/NNN-*.yaml`, each listed in
`src/indexer/db/liquibase/changelog/db.changelog-master.yaml`. Never edit an existing changeset: add a new numbered file,
list it in the master changelog, and run `docker compose run --rm liquibase`. Liquibase 5 ships no
JDBC drivers, so `src/indexer/db/liquibase/liquibase.Dockerfile` adds the PostgreSQL driver at a pinned version,
verified by its SHA-256 checksum.
