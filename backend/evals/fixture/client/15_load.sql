-- Server-side COPY of the generator's files (superuser, init phase). FK order.
COPY shop.regions      (id, name, zone) FROM '/seed/regions.copy';
COPY shop.customers    (id, full_name, email, phone, city, region_id, segment, signup_at) FROM '/seed/customers.copy';
COPY shop.products     (id, sku, name, category, unit_price, is_active) FROM '/seed/products.copy';
COPY shop.orders       (id, customer_id, status, channel, ordered_at, delivered_on, total_amount) FROM '/seed/orders.copy';
COPY shop.order_items  (order_id, line_no, product_id, quantity, unit_price) FROM '/seed/order_items.copy';
COPY shop.refunds      (id, order_id, amount, reason, refunded_at) FROM '/seed/refunds.copy';
COPY shop."Order Events" (id, "order", "Event Type", occurred_at) FROM '/seed/order_events.copy';

-- Deterministic stand-in rows for the column-grant table (email/phone must exist to be withheld).
INSERT INTO shop.customer_contacts (id, city, region_id, email, phone)
SELECT id, city, region_id, email, phone FROM shop.customers ORDER BY id LIMIT 50;
