#!/bin/sh
# Builds the runner process environment from the per-run secrets (INV-P0-02), checks the
# host guard (INV-P0-01), migrates the disposable app DB to head, then runs the command.
set -eu

. /secrets/secrets.env

export APP_ENV=dev
export API_KEY="${NN_API_KEY}"
export DATABASE_URL="postgresql+asyncpg://nn_app:${NN_APP_DB_PASSWORD}@app-postgres.nn-eval.test:5432/nn_app"
export CONNECTORS_ENABLED=true
export CONNECTOR_EGRESS_POLICY_CONFIGURED=true
export CONNECTOR_CREDENTIALS_ACTIVE_KEY_ID="eval-${NN_RUN_ID}"
export CONNECTOR_CREDENTIALS_ACTIVE_KEY="${NN_FERNET_KEY}"
export JWT_SECRET_KEY="${NN_JWT_SECRET}"
export NN_EVAL_CLIENT_HOST=client-postgres.nn-eval.test
export NN_CA_PEM_PATH=/certs/ca.crt
export NN_READER_PASSWORD NN_GRADER_PASSWORD
export PYTHONPATH=/app

cd /app
python - <<'PY'
from evals import guards
guards.assert_fixture_environment()
PY

out=$(alembic upgrade head 2>&1) || { echo "$out" >&2; exit 1; }
exec "$@"
