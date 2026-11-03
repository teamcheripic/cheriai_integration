#!/bin/zsh
# Applies migrations 056–059 twice to a THROWAWAY local Postgres (stub
# Supabase schema) and runs the behaviour checks in tests.sql.
# Never point this at a real database.
# Usage: start a local postgres on 127.0.0.1:54329 (user postgres), then
#   zsh cheriai_integration/fastapi/tests/sql/run_all.sh
set -u
HERE=${0:A:h}; MIG=${HERE:h:h}/migrations
export PGHOST=127.0.0.1 PGPORT=${PGPORT:-54329} PGUSER=postgres
dropdb --if-exists cp_test && createdb cp_test || exit 1
psql -d cp_test -q -v ON_ERROR_STOP=1 -f $HERE/stub.sql -f $HERE/seed.sql || exit 1
for run in 1 2; do for f in 056_age_policy_consent 057_moderation 058_country_billing 059_admin_mfa; do
  psql -d cp_test -q -v ON_ERROR_STOP=1 -f $MIG/$f.sql >/dev/null 2>&1 || { echo "apply run $run $f FAILED"; exit 1; }
  echo "apply run $run $f ok"
done; done
psql -d cp_test -f $HERE/tests.sql 2>&1 | grep -E '^(PASS|FAIL|===)'
