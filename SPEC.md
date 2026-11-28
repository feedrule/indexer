# Output files

| Path | Contents |
| --- | --- |
| `topic/topics.txt` | Published topic names, one per line, most feeds first. |
| `topic/<slug>/links.txt` | Feed URLs, one per line, newest post first (at most 50,000). |
| `topic/<slug>/feeds.opml` | OPML 2.0, the 500 most recently active feeds, by URL. |
| `all/links.txt` | Every listed feed, with a topic or not, newest post first. |
| `all/feeds.opml` | OPML 2.0 of every listed feed, sorted by URL. |
| `previews/<h:2>/<h>.json` | One JSON file per listed feed (see below). |
| `docs/index.html` | Website (GitHub Pages serves `docs/`): the 50 largest topics, a search over all published topics, and the sources. |
| `docs/topic/<slug>/index.html` | Page of the first 200 of those feeds (20 shown at first, then 20 more per "Show more" click): title (links to the website), the domain under it, the feed's subtitle (up to 200 characters), "Website", "Feed" (links to the feed), "Preview" and "Email digest" (feedrule.com's form, prefilled with the feed) on one line, the feed's other topics (most relevant first), last post date. |
| `docs/preview/index.html` | The feed preview page, `preview/?sha=<h>` (nothing else is accepted): a script fetches that feed's preview and posts file and shows its title, subtitle, links, languages, where it was imported from (linked to the index's sources table), topics and recent posts (each description cut at 300 characters). Marked `noindex`, not in the sitemap. |
| `docs/style.css`, `docs/feedrule.svg` | The site's stylesheet and feedrule.com's icon. |
| `docs/sitemap.xml` | The index and every topic page with at least 3 feeds (smaller ones are marked `noindex`). |
| `docs/.nojekyll` | Tells GitHub Pages to serve the files as they are. |

`<h>` is the SHA-256 (hex) of the feed URL in UTF-8; `<h:2>` is its first 2 characters.

Topic names are lower-case `a-z`, `0-9`, `-`, `_`, `+` and `#` (`rust`, `web-development`,
`c++`). A topic's `<slug>` is its name with `+` spelled `plus` and `#` spelled `sharp`
(`c++` -> `cplusplus`, `c#` -> `csharp`).

Preview fields: `url` (the feed URL), `title`, `subtitle` (the feed's description), `site` (the feed's website, `http(s)` only), `bytes` (feed size at the last successful check),
`last` (date of the newest post, `YYYY-MM-DD`), `languages` (the languages detected on its posts,
as ISO 639 codes, sorted; empty if none), `topics` (topic names, most relevant first, at most 5;
empty if none stands out; only those in `topic/topics.txt` have a folder), `source` (the list the
feed was found in, as the site's sources table shows it: `name`, e.g. `kagisearch/smallweb`,
and `license`, e.g. `MIT`, `CC0-1.0`, `Common Crawl terms`; `name` may be `null`, then
`license` is an SPDX id), `posts_url` (the feed's posts file on the `posts` branch, see below;
`null` if it has none). `title`, `subtitle`, `site`, `bytes`, `last`, `source` and `posts_url` may be
`null`.

## Posts

The feeds' recent posts are on the `posts` branch (served at
`https://raw.githubusercontent.com/feedrule/indexer/posts/`), one JSON file per feed at
`data/posts/<h:2>/<h>.json` (the same `<h>` as its preview). A file is kept after the feed's posts are
cleared from the database, so it holds the latest posts seen. Fields: `url` (the feed URL),
`posts` (up to 3 of the feed's most recent posts,
newest first, each `link`, `title`, `description` (the start of the post) and `date` (the post's
newest date, `YYYY-MM-DD`, UTC); any of them may be `null`, and only `http(s)` links are given).

Titles, subtitles and posts come from the feeds: treat them as untrusted plain text, never as
HTML.
