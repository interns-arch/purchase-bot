-- Runs ONCE, when the data directory is first created. Never on a restart,
-- and never against a database that already has data.
--
-- This setting DOES change stored values, and that is now deliberate.
--
-- Every model defaults its timestamps with `server_default=func.now()`,
-- which Postgres evaluates -- so this line decides what those columns
-- contain. The application's storage convention is naive IST to match
-- (core/time_utils.py). Between Aug and 9 Sep 2026 the comment here claimed
-- "nothing here changes a stored value" while `to_ist()` assumed UTC, and
-- the two disagreed by 5h30 in the UI.
--
-- It also makes the DATABASE agree with the application when a human looks
-- directly at it: psql output, now(), and the server log all read in IST,
-- which is what you want when checking "what did the 09:15 job actually do
-- this morning".
--
-- ALTER DATABASE needs a literal name and the entrypoint defines no psql
-- variables, so the current database name is resolved at runtime instead of
-- being hardcoded (POSTGRES_DB is configurable in docker-compose.yml).

DO $$
BEGIN
    EXECUTE format(
        'ALTER DATABASE %I SET timezone TO %L', current_database(), 'Asia/Kolkata'
    );
END
$$;

-- Deliberately NOT set: statement_timeout. Importing a 25,000-row vendor
-- file is one long transaction and must never be cut off part-way.
