-- Roles only; passwords are applied by 05_passwords.sh from the per-run secrets (INV-P0-02).
CREATE ROLE nn_owner NOLOGIN;

CREATE ROLE nn_reader LOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- SPEC-05 drift tests: owns scratch tables in the `drift` schema only, so a test can drop a column,
-- revoke SELECT, enable RLS, swap in a view or change an owner without superuser rights.
CREATE ROLE nn_drift LOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Runs trusted reference SQL only (INV-P0-08). Read-only by default.
CREATE ROLE nn_grader LOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE nn_grader SET default_transaction_read_only = on;
ALTER ROLE nn_grader SET statement_timeout = '120s';
