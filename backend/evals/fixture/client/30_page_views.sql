-- 2,000,000 rows, pure arithmetic from the row number (no random()): fast, deterministic, expensive to scan.
SET max_parallel_workers_per_gather = 0;

INSERT INTO shop.page_views (id, customer_id, path, viewed_at)
SELECT g,
       CASE WHEN g % 11 = 0 THEN NULL ELSE c.ids[1 + ((g::bigint * 7919) % 5000)::int] END,
       (ARRAY['/', '/products', '/cart', '/checkout', '/search', '/account'])[1 + (g % 6)]
         || CASE WHEN g % 6 = 1 THEN '/' || (1 + ((g::bigint * 37) % 400)) ELSE '' END,
       -- Rows 1-3 sit exactly on the boundaries the golden questions use (>= vs > must matter).
       CASE g
         WHEN 1 THEN TIMESTAMPTZ '2025-01-01 00:00:00+05:30'
         WHEN 2 THEN TIMESTAMPTZ '2026-01-01 00:00:00+05:30'
         WHEN 3 THEN TIMESTAMPTZ '2026-06-14 23:00:00+05:30'     -- path index 1 + (3 % 6) = '/checkout'
         ELSE TIMESTAMPTZ '2024-01-01 00:00:00+05:30' + ((g::bigint * 397) % 77414400) * INTERVAL '1 second'
       END
FROM generate_series(1, 2000000) AS g,
     (SELECT array_agg(id ORDER BY id) AS ids FROM shop.customers) AS c;

ANALYZE;
