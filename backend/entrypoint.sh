#!/bin/sh
set -e
export PYTHONPATH="/app/libs:${PYTHONPATH}"
echo "Running database migrations..."
alembic upgrade head
echo "Starting server..."
exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
