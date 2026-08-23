#!/bin/sh
# Create a throwaway database for the test suite and give it the same
# schema. The suite truncates every table, so it must never point at the
# development database -- see tests/test_loader.py.
#
# Runs before 01-schema.sql (hence the 00- prefix); Postgres applies
# that one to $POSTGRES_DB only.
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
     -c "CREATE DATABASE bookrs_test OWNER $POSTGRES_USER;"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d bookrs_test \
     -f /docker-entrypoint-initdb.d/01-schema.sql
