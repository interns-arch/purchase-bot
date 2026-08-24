#!/usr/bin/env bash
# Post-deployment verification. Read-only: it never writes to the database
# and never restarts anything.
#
#   ./deploy/verify-deployment.sh
#
# Covers the checks that matter after a deploy: containers healthy, Postgres
# on 18 with the right volume and timezone, the restored data present, the
# backend talking to the CONTAINER database (not Neon), Nginx routing, and
# Postgres NOT reachable from outside.

set -uo pipefail

COMPOSE=(docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml)
DB_USER="${POSTGRES_USER:-procurehub}"
DB_NAME="${POSTGRES_DB:-procurehub}"
PASS=0; FAIL=0

ok()   { printf '  \033[32m[PASS]\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
check(){ if [ "$1" = "true" ]; then ok "$2"; else bad "$2 ${3:+-- $3}"; fi; }

echo "=============================================================="
echo "1) CONTAINERS"
"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Status}}'
for svc in db backend frontend; do
    status=$("${COMPOSE[@]}" ps "$svc" --format '{{.Status}}' 2>/dev/null)
    case "$status" in
        *healthy*|*Up*) ok "$svc is up ($status)";;
        *) bad "$svc is not up" "$status";;
    esac
done

echo
echo "2) POSTGRES"
VER=$("${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -tAc "show server_version" 2>/dev/null)
case "$VER" in 18.*) ok "server version $VER";; *) bad "expected 18.x" "$VER";; esac

TZ_DB=$("${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -tAc "show timezone" 2>/dev/null)
check "$([ "$TZ_DB" = "Asia/Kolkata" ] && echo true || echo false)" "timezone is Asia/Kolkata" "$TZ_DB"

PGDATA_DIR=$("${COMPOSE[@]}" exec -T db sh -c 'echo $PGDATA' 2>/dev/null | tr -d '\r')
check "$(case "$PGDATA_DIR" in /var/lib/postgresql/18/*) echo true;; *) echo false;; esac)" \
      "PGDATA uses the Postgres 18 layout" "$PGDATA_DIR"

MOUNT=$(docker inspect "$("${COMPOSE[@]}" ps -q db)" \
        --format '{{range .Mounts}}{{.Name}}{{end}}' 2>/dev/null)
check "$([ "$MOUNT" = "procurehub_pgdata18" ] && echo true || echo false)" \
      "data volume is procurehub_pgdata18" "$MOUNT"

echo
echo "3) RESTORED DATA"
read_count() {
    "${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -tAc "select count(*) from $1" 2>/dev/null | tr -d '\r'
}
TABLES=$("${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" \
    -tAc "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null | tr -d '\r')
check "$([ "${TABLES:-0}" -eq 38 ] && echo true || echo false)" "38 tables" "found ${TABLES:-0}"
VENDORS=$(read_count vendors)
check "$([ "${VENDORS:-0}" -eq 54 ] && echo true || echo false)" "54 vendors" "found ${VENDORS:-0}"
for t in users customers parts vendor_inventory customer_orders; do
    n=$(read_count "$t")
    check "$([ "${n:-0}" -gt 0 ] && echo true || echo false)" "$t has rows" "found ${n:-0}"
done

echo
echo "4) BACKEND"
BACKEND_PORT="${BACKEND_PORT:-4008}"
FRONTEND_PORT="${FRONTEND_PORT:-4009}"
HEALTH=$(curl -fsS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${BACKEND_PORT}/health" 2>/dev/null)
check "$([ "$HEALTH" = "200" ] && echo true || echo false)" "/health returns 200" "got ${HEALTH:-no response}"

DBURL=$("${COMPOSE[@]}" exec -T backend sh -c 'echo $DATABASE_URL' 2>/dev/null | tr -d '\r')
case "$DBURL" in
    *"@db:5432"*) ok "backend points at the container database (db:5432)";;
    *neon.tech*)  bad "BACKEND IS POINTING AT NEON -- fix before going live";;
    *)            bad "unexpected DATABASE_URL host" "${DBURL%%:*}...";;
esac
ERRORS=$("${COMPOSE[@]}" logs backend --tail 200 2>/dev/null | grep -ciE 'OperationalError|could not connect|FATAL')
check "$([ "${ERRORS:-0}" -eq 0 ] && echo true || echo false)" "no database errors in recent backend logs" "$ERRORS found"

echo
echo "5) NGINX / PUBLIC SURFACE"
NG=$(curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/ 2>/dev/null)
check "$([ "$NG" = "200" ] && echo true || echo false)" "Nginx serves the frontend on :80" "got ${NG:-no response}"
NGAPI=$(curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1/health 2>/dev/null)
check "$([ "$NGAPI" = "200" ] && echo true || echo false)" "Nginx proxies /health to the backend" "got ${NGAPI:-no response}"

# Postgres must not be listening on any public interface.
if command -v ss >/dev/null 2>&1; then
    PUBLIC_PG=$(ss -ltn 2>/dev/null | grep -E ':(5432|55432)\b' | grep -v '127.0.0.1' | wc -l)
    check "$([ "$PUBLIC_PG" -eq 0 ] && echo true || echo false)" \
          "Postgres is NOT listening on a public interface" "$PUBLIC_PG socket(s) found"

    # 4008/4009 must answer on loopback ONLY -- they are deliberately absent
    # from the Security Group, so a 0.0.0.0 binding here would be a hole.
    for p in "$BACKEND_PORT" "$FRONTEND_PORT"; do
        PUBLIC_APP=$(ss -ltn 2>/dev/null | grep -E ":${p}\b" | grep -vc '127.0.0.1')
        check "$([ "$PUBLIC_APP" -eq 0 ] && echo true || echo false)" \
              "port $p is loopback-only (not public)" "$PUBLIC_APP public socket(s)"
    done
fi
PUB_PORTS=$(docker ps --format '{{.Ports}}' | grep -oE '0\.0\.0\.0:[0-9]+' | sort -u | tr '\n' ' ')
check "$([ -z "$PUB_PORTS" ] && echo true || echo false)" \
      "no container publishes on 0.0.0.0" "${PUB_PORTS:-none}"

echo
echo "=============================================================="
echo "PASSED: $PASS   FAILED: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
