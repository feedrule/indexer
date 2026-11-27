-- Feeds with a successful check in the last success_days days whose newest item, as of their
-- latest successful check, is less than item_years years old. Columns named like ListedFeed.
WITH latest_ok AS (
    SELECT DISTINCT ON (url) url, last_item_date, file_size, recent_posts
    FROM health_checks
    WHERE error IS NULL AND checked_at > now() - make_interval(days => %(success_days)s)
    ORDER BY url, checked_at DESC
)
SELECT f.url, f.title, f.site_url, l.last_item_date, f.language, l.file_size, f.topics,
       f.subtitle, i.source, i.source_license, l.recent_posts
FROM feeds f JOIN latest_ok l USING (url) LEFT JOIN inputs i USING (url)
WHERE l.last_item_date > now() - make_interval(years => %(item_years)s)
