#!/bin/sh
# SPEC-03 test F9: tear the fixture down, rebuild it from scratch, and prove every table checksum equals the
# committed manifest. Needs the Docker socket, so it runs on the host / in CI, not inside the runner.
set -eu
COMPOSE="docker compose -f backend/evals/fixture/compose.yml"
$COMPOSE down -v
$COMPOSE up -d --build
$COMPOSE run --rm runner python -m evals.checksums --check
