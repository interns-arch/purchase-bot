# ProcureHub database.
#
# A thin, pinned layer over the official Postgres image rather than pulling
# `postgres:18-alpine` directly, so the database is versioned with the
# application like the backend and frontend images are: the Postgres version,
# the timezone and any initialisation live in this repo and change through a
# reviewed commit.
#
# What it adds:
#   * TIMEZONE = Asia/Kolkata, matching the application. The app already
#     stores UTC and converts for display (core/time_utils.py), so this
#     changes no stored value -- it makes `psql` output, `now()` in an ad-hoc
#     query, and the server log timestamps read in IST instead of UTC, which
#     is what anyone debugging "what happened at 9:15 this morning" expects.
#   * An init hook directory. Anything in docker-entrypoint-initdb.d runs
#     ONCE, when the data volume is first created -- never on later starts,
#     so it can never overwrite an existing database.
#
# The application still creates its own tables at startup (core/db.py
# `create_all`), so this image deliberately does NOT define the schema:
# two sources of truth for the schema would drift.

FROM postgres:18-alpine

# Server-side timezone. POSTGRES_* variables are read by the entrypoint; TZ
# is what the server itself and its logs use.
ENV TZ=Asia/Kolkata \
    PGTZ=Asia/Kolkata

# The alpine image is built on musl and ships no zone database.
RUN apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/Asia/Kolkata /etc/localtime \
    && echo "Asia/Kolkata" > /etc/timezone

# Runs once, on first initialisation of an empty data directory.
COPY initdb/ /docker-entrypoint-initdb.d/

# Inherited from the base image: EXPOSE 5432, the entrypoint, and the
# postgres user. Compose supplies POSTGRES_USER / _PASSWORD / _DB and the
# pg_isready healthcheck.
