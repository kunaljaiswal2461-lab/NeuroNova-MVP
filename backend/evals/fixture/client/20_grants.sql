-- SPEC-03 §8.4. Hand every object to nn_owner, then grant the minimum.
DO $$
DECLARE r record;
BEGIN
  EXECUTE 'ALTER SCHEMA shop OWNER TO nn_owner';
  EXECUTE 'ALTER SCHEMA internal OWNER TO nn_owner';
  FOR r IN SELECT schemaname AS s, tablename AS n FROM pg_tables WHERE schemaname IN ('shop', 'internal') LOOP
    EXECUTE format('ALTER TABLE %I.%I OWNER TO nn_owner', r.s, r.n);
  END LOOP;
  FOR r IN SELECT schemaname AS s, viewname AS n FROM pg_views WHERE schemaname IN ('shop', 'internal') LOOP
    EXECUTE format('ALTER VIEW %I.%I OWNER TO nn_owner', r.s, r.n);
  END LOOP;
  FOR r IN SELECT schemaname AS s, matviewname AS n FROM pg_matviews WHERE schemaname IN ('shop', 'internal') LOOP
    EXECUTE format('ALTER MATERIALIZED VIEW %I.%I OWNER TO nn_owner', r.s, r.n);
  END LOOP;
END $$;

-- Nothing implicit: no PUBLIC temp/create, no PUBLIC schema access.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA shop, internal FROM PUBLIC;
REVOKE ALL ON DATABASE nn_client FROM PUBLIC;
GRANT CONNECT ON DATABASE nn_client TO nn_reader, nn_grader;

GRANT USAGE ON SCHEMA shop TO nn_reader, nn_grader;          -- internal: deliberately no USAGE

GRANT SELECT ON shop.regions, shop.customers, shop.products, shop.orders, shop.order_items,
                shop.refunds, shop."Order Events", shop.page_views
  TO nn_reader, nn_grader;

-- Objects Part 1/Part 2 must refuse or handle (not in the source's selected scope):
GRANT SELECT (id, city, region_id) ON shop.customer_contacts TO nn_reader;   -- column-level only
GRANT SELECT ON shop.v_daily_sales, shop.mv_monthly_sales, shop.rls_notes, shop.events_partitioned TO nn_reader;
-- shop.audit_log: no grant at all.
