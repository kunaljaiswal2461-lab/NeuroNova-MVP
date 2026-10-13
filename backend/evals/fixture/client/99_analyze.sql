-- SPEC-05 B7: plan-cost checks (EXPLAIN total cost) are only meaningful with planner statistics.
-- Runs last so every table loaded or derived above, in any init file, has statistics.
ANALYZE;
