# PostgreSQL source onboarding and catalog discovery (Stages 2B and 3B)

Stage 2B connects only long enough to test a supplied PostgreSQL connection
and list eligible table *names*. Stage 3B adds a separately operated, manual
structural-metadata discovery worker for a saved source and its explicit
table scope. Neither stage ingests client data, profiles tables, executes
analytical SQL, or exposes database chat. See
[the Stage 3B worker runbook](stage3b-catalog-discovery.md) for its API and
operational contract.

## Activation and configuration

The API is disabled by default. Enabling it requires all of the following
deployment configuration, supplied through the deployment platform's encrypted
environment-variable facility:

```dotenv
CONNECTORS_ENABLED=true
CONNECTOR_EGRESS_POLICY_CONFIGURED=true
CONNECTOR_CREDENTIALS_ACTIVE_KEY_ID=<operator-managed-key-id>
CONNECTOR_CREDENTIALS_ACTIVE_KEY=<Fernet-key>
# Optional, temporary JSON mapping of old key IDs to Fernet keys:
CONNECTOR_CREDENTIALS_PREVIOUS_KEYRING=<secret-json-object>
```

`CONNECTOR_CREDENTIALS_ACTIVE_KEY` is a URL-safe, base64 Fernet key generated
and retained by an operator. It is not the JWT signing key, is never generated
by the application, and must not be placed in the application database.
`CONNECTOR_CREDENTIALS_PREVIOUS_KEYRING` permits no more than three prior keys
during a rotation. It is secret material too.

For Docker Compose, root `.env` is used by the Compose `env_file`. For direct
backend execution, the settings module reads `backend/.env`; platform-injected
environment variables override either. The tracked `.env.example` contains
names only—never put working connector keys there.

## Credential storage and rotation

The application stores the non-secret source settings in `data_sources` and a
Fernet-authenticated credential bundle in the one-to-one `source_credentials`
row. The bundle contains the username, password, optional custom CA PEM, and
the source UUID. Decryption verifies that embedded UUID against the expected
row, preventing ciphertext substitution between sources.

Direct Fernet encryption is used here; this is **not envelope encryption**.
There is no separately generated data key or wrapping key.

To rotate:

1. Add the new key and ID as active while retaining the old key in the bounded
   prior-key keyring.
2. Re-encrypt existing rows with an operator-run maintenance process that calls
   `CredentialProtector.reencrypt`; it validates each existing bundle and is
   safe to rerun because active-key rows remain unchanged.
3. Verify that no row references the old ID, then remove the old key.

Stage 2B does not expose credential rotation through HTTP and does not run any
rotation process automatically. Never remove a historical key before all rows
using it have been rewrapped. Missing key IDs, malformed ciphertext, and
unsupported formats fail closed. Source listing/deletion remains possible
without decryption.

## Supported connection boundary

- Public DNS hostname only; literal IP addresses and database URLs are rejected.
- TCP port `5432` only.
- TLS is mandatory with certificate-chain and hostname verification
  (`verify-full`). A custom CA PEM is optional and remains encrypted at rest.
- No TLS-disable mode, client certificate, SSH tunnel, private-network route,
  Unix socket, proxy option, URI/DSN option, or arbitrary connection option.
- The adapter uses a short-lived `asyncpg` connection, a five-second connect
  timeout, five-second statement timeout, ten-second operation timeout, and
  always closes the connection.

The application resolves and rejects private, loopback, link-local, multicast,
reserved, unspecified, CGNAT, and non-global IPv4/IPv6 addresses before it
connects. `asyncpg` resolves the hostname again when opening its socket, so
this preflight check alone cannot eliminate DNS rebinding. Before setting both
activation flags in production, operators must deploy an egress firewall or
proxy that performs destination enforcement at connection time, permits only
the intended public PostgreSQL egress, and does not block application-database
traffic. This repository contains no proof that a Render deployment has such
an egress control or a stable outbound IP.

CORS is limited to `FRONTEND_ALLOWED_ORIGINS`; CORS is not an authorization or
egress-security boundary. Source endpoints require an application JWT. The
legacy global API key is not accepted.

## Client database role

Use a dedicated, non-admin role with only `CONNECT`, schema `USAGE`, and
table-level `SELECT` on the tables intended for onboarding. It must not have
write, DDL, role-management, replication, or broad `pg_read_all_data`
privileges. Inventory includes only non-system `BASE TABLE`s for which the
role has schema usage, table-level `SELECT`, and `SELECT` for every current
column. Views, materialized views, foreign tables, system schemas, and
column-only grants are excluded intentionally. Grant table-level `SELECT` if a
table should be selectable; column-only permission models are not supported in
this onboarding release.

Inventory is capped at 1,000 tables and overflows fail instead of silently
truncating a schema selection. Only table names are returned. No rows,
samples, column descriptions, or detailed metadata are fetched.

## API sequence

1. A JWT user sends transient connection settings and credential form fields to
   `POST /api/v1/sources/postgresql/test`.
2. The server tests the connection and returns eligible names only; it persists
   no draft or credential.
3. The browser holds credentials only in transient form/component memory while
   the user selects explicit tables.
4. `POST /api/v1/sources` receives the form again plus the explicit selection.
   The server inventories again, verifies every selected table, then atomically
   stores the source, encrypted credential bundle, and explicit scope rows.

Selecting a schema in the future UI must expand to its currently listed table
names. There are no schema wildcards, so subsequently discovered tables remain
excluded until selected. `profiling_enabled` is permanently false in Stage 2B.
