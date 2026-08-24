-- Runs ONCE, when the data directory is first created. Never on a restart,
-- and never against a database that already has data.
--
-- The application stores UTC and converts to IST for display
-- (core/time_utils.py), so nothing here changes a stored value. This makes
-- the DATABASE agree with the application when a human looks directly at it:
-- psql output, now(), and the server log all read in IST, which is what you
-- want when checking "what did the 09:15 job actually do this morning".
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
