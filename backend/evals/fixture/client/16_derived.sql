-- Objects derived from loaded data (SPEC-03 §8.5): a view and a materialized view.
CREATE VIEW shop.v_daily_sales AS
SELECT (ordered_at AT TIME ZONE 'Asia/Kolkata')::date AS sales_date,
       count(*)           AS orders,
       sum(total_amount)  AS gross_amount
FROM shop.orders
WHERE status = 'PAID'
GROUP BY 1;

CREATE MATERIALIZED VIEW shop.mv_monthly_sales AS
SELECT date_trunc('month', ordered_at AT TIME ZONE 'Asia/Kolkata')::date AS sales_month,
       count(*)           AS orders,
       sum(total_amount)  AS gross_amount
FROM shop.orders
WHERE status = 'PAID'
GROUP BY 1;
