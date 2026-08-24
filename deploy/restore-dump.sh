#!/usr/bin/env bash
# Restore procurehub.dump into the LOCAL Docker Postgres container.
#
#   ./deploy/restore-dump.sh procurehub.dump
#
# Every guard here exists because restoring into the wrong database, or into
# one that already has tables, is unrecoverable without another dump.
#
# GUARDS, in order:
#   1. Refuses unless the target is the compose `db` CONTAINER. It never
#      takes a host/URL, so it cannot reach Neon even by mistake.
#   2. Refuses if DATABASE_URL anywhere in the environment points at Neon.
#   3. Refuses if the target database already contains application tables,
#      unless you pass --clean (which you must type deliberately).
#   4. Prints the server version and the target, and waits for you to type
#      the database name before writing anything.
#
# The backend must NOT be running: it creates the 38 empty tables on
# startup, which would collide with the dump.

set -euo pipefail

DUMP_FILE="${1:-procurehub.dump}"
CLEAN_MODE="${2:-}"
COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
DB_SERVICE="db"
DB_USER="${POSTGRES_USER:-procurehub}"
DB_NAME="${POSTGRES_DB:-procurehub}"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
green(){ printf '\033[32m%s\033[0m\n' "$*"; }

[ -f "$DUMP_FILE" ] || { red "Dump file not found: $DUMP_FILE"; exit 1; }

# --- Guard 2: never, ever Neon -------------------------------------------
if [[ "${DATABASE_URL:-}" == *"neon.tech"* ]]; then
    red "REFUSING: DATABASE_URL in this shell points at Neon."
    red "This script only ever restores into the local Docker container."
    exit 1
fi

echo "==> Target container: compose service '$DB_SERVICE' (never a remote host)"
"${COMPOSE[@]}" ps "$DB_SERVICE" --format 'table {{.Service}}\t{{.Status}}'

SERVER_VERSION=$("${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    psql -U "$DB_USER" -d "$DB_NAME" -tAc "show server_version")
echo "==> Postgres server: $SERVER_VERSION"
case "$SERVER_VERSION" in
    18.*) ;;
    *) red "REFUSING: expected Postgres 18.x, found $SERVER_VERSION"; exit 1;;
esac

# --- Guard 3: the database must be empty ---------------------------------
TABLE_COUNT=$("${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" \
    -tAc "select count(*) from information_schema.tables where table_schema='public'")
echo "==> Tables currently in '$DB_NAME': $TABLE_COUNT"

if [ "$TABLE_COUNT" -ne 0 ] && [ "$CLEAN_MODE" != "--clean" ]; then
    red "REFUSING: '$DB_NAME' already contains $TABLE_COUNT table(s)."
    echo
    echo "Most likely the backend started first and created its empty tables."
    echo "Either:"
    echo "  * stop the backend and drop the schema, then re-run:"
    echo "      ${COMPOSE[*]} stop backend"
    echo "      ${COMPOSE[*]} exec db psql -U $DB_USER -d $DB_NAME \\"
    echo "          -c 'drop schema public cascade; create schema public;'"
    echo "  * or re-run this script with --clean to let pg_restore replace them:"
    echo "      $0 $DUMP_FILE --clean"
    exit 1
fi

# --- Guard 4: confirm out loud -------------------------------------------
echo
echo "About to restore:"
echo "  dump   : $DUMP_FILE ($(du -h "$DUMP_FILE" | cut -f1))"
echo "  into   : container service '$DB_SERVICE', database '$DB_NAME'"
echo "  mode   : ${CLEAN_MODE:-fresh (database is empty)}"
echo
echo "This does NOT touch Neon. Type the database name to continue:"
read -r CONFIRM
[ "$CONFIRM" = "$DB_NAME" ] || { red "Aborted."; exit 1; }

echo "==> Copying the dump into the container"
"${COMPOSE[@]}" cp "$DUMP_FILE" "$DB_SERVICE:/tmp/procurehub.dump"

echo "==> Restoring (pg_restore)"
RESTORE_ARGS=(-U "$DB_USER" -d "$DB_NAME" --no-owner --no-privileges)
[ "$CLEAN_MODE" = "--clean" ] && RESTORE_ARGS+=(--clean --if-exists)
# pg_restore exits non-zero on harmless notices (e.g. a missing extension
# owner); capture the status rather than letting `set -e` abort mid-way.
set +e
"${COMPOSE[@]}" exec -T "$DB_SERVICE" pg_restore "${RESTORE_ARGS[@]}" /tmp/procurehub.dump
RESTORE_STATUS=$?
set -e
[ $RESTORE_STATUS -eq 0 ] || echo "(pg_restore exited $RESTORE_STATUS -- check the messages above; \
warnings about owners/privileges are expected with --no-owner)"

echo "==> Removing the dump from the container"
"${COMPOSE[@]}" exec -T "$DB_SERVICE" rm -f /tmp/procurehub.dump

echo
green "==> Restore finished. Verifying:"
"${COMPOSE[@]}" exec -T "$DB_SERVICE" psql -U "$DB_USER" -d "$DB_NAME" -c "
    select 'tables'            as record, count(*) from information_schema.tables where table_schema='public'
    union all select 'vendors',           count(*) from vendors
    union all select 'users',             count(*) from users
    union all select 'customers',         count(*) from customers
    union all select 'parts',             count(*) from parts
    union all select 'vendor_inventory',  count(*) from vendor_inventory
    union all select 'customer_orders',   count(*) from customer_orders
    union all select 'purchase_orders',   count(*) from vendor_purchase_orders;"

echo
echo "Expected from the migrated production database: 38 tables, 54 vendors."
echo "If those match, start the backend:"
echo "  ${COMPOSE[*]} up -d backend frontend"
