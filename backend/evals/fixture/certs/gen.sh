#!/bin/sh
# Generates the per-run test CA, the server certificate and every per-run secret (INV-P0-02).
# Output lives only in Docker volumes; nothing here is, or may become, committed.
set -eu
umask 077
HOST=client-postgres.nn-eval.test

# `docker compose run` re-runs completed one-shot dependencies. Regenerating here would rotate the
# secrets and CA under databases that are already running, so material is created once per volume
# (a fresh `down -v` / `up` gives a fresh set) and reused afterwards.
if [ -s /certs/server.crt ] && [ -s /secrets/secrets.env ]; then
  if openssl x509 -checkend 86400 -noout -in /certs/server.crt >/dev/null; then
    echo "certs + secrets already present; reusing"
    exit 0
  fi
  echo "fixture certificate expires within a day: run 'docker compose down -v' and bring it up again" >&2
  exit 1
fi

rm -rf /certs/* /secrets/*
cd /certs

# EC P-256 CA, valid for 2 days.
openssl ecparam -name prime256v1 -genkey -noout -out ca.key
openssl req -new -x509 -key ca.key -sha256 -days 2 -subj "/CN=nn-eval-test-ca" -out ca.crt

# Server certificate with SAN = the client alias, signed by that CA.
openssl ecparam -name prime256v1 -genkey -noout -out server.key
openssl req -new -key server.key -subj "/CN=${HOST}" -out server.csr
printf 'subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=serverAuth\n' "$HOST" > ext.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 2 -sha256 \
  -extfile ext.cnf -out server.crt

# The CA key is never needed again.
rm -f ca.key ca.srl server.csr ext.cnf

# postgres (uid 999 in the official image) must own the key, mode 0600.
chown 999:999 server.key server.crt
chmod 600 server.key
chmod 644 server.crt ca.crt

# Per-run secrets. URL-safe base64 so they are safe in SQL literals and URLs.
rand() { openssl rand -base64 "$1" | tr -d '\n=' | tr '+/' '-_'; }
cd /secrets
rand 32 > client_superuser_password
rand 32 > app_db_password
chmod 644 client_superuser_password app_db_password
{
  echo "NN_RUN_ID=$(date -u +%Y%m%d%H%M%S)-$(openssl rand -hex 3)"
  echo "NN_READER_PASSWORD=$(rand 32)"
  echo "NN_GRADER_PASSWORD=$(rand 32)"
  echo "NN_APP_DB_PASSWORD=$(cat /secrets/app_db_password)"
  echo "NN_API_KEY=$(rand 24)"
  # A Fernet key is 32 url-safe-base64 bytes, '=' padding included.
  echo "NN_FERNET_KEY=$(openssl rand -base64 32 | tr -d '\n' | tr '+/' '-_')"
  echo "NN_JWT_SECRET=$(rand 48)"
} > secrets.env
chmod 644 secrets.env
echo "certs + secrets generated"
