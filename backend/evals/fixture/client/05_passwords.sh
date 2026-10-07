#!/bin/sh
# Applies the generated role passwords through the local trust socket during init.
set -eu
. /secrets/secrets.env
psql -v ON_ERROR_STOP=1 -U postgres -d nn_client <<EOSQL
ALTER ROLE nn_reader PASSWORD '${NN_READER_PASSWORD}';
ALTER ROLE nn_grader PASSWORD '${NN_GRADER_PASSWORD}';
EOSQL
