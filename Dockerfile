# ProcureHub backend (FastAPI + the shared core/ business logic).
#
# Two things this image must get right, both learned from how the app
# actually runs:
#
#  1. It runs from the PROJECT ROOT. `backend.app.main` imports `core.*`,
#     so both packages sit side by side at /app and uvicorn is started with
#     the dotted path -- never from inside backend/.
#  2. tzdata is REQUIRED, not optional. The scheduler hands the string
#     "Asia/Kolkata" to APScheduler (backend/app/workers/scheduler.py) and
#     core/time_utils.py asks for ZoneInfo("Asia/Kolkata"); without the IANA
#     database the daily 09:30 stock request and the 09:15 sheet reset would
#     silently fail to schedule.

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata \
    # matplotlib is pulled in by requirements.txt and writes a config dir on
    # first import; point it somewhere writable for the non-root user.
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app

# tzdata: see note above. curl: used by the container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a code change does not re-run pip install.
# backend/requirements.txt starts with `-r ../requirements.txt`, so BOTH
# files must be copied with their real paths intact.
COPY requirements.txt ./requirements.txt
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Application code. `core` is the shared business logic the backend imports;
# it is not a sub-package of `backend`, hence two COPYs.
COPY core ./core
COPY backend ./backend

# Runtime-writable paths. The app creates these itself on demand, but making
# them here means they exist with the right owner even when a fresh named
# volume is mounted over them.
#   uploads/   incoming -> processed | failed  (staging.py)
#   database/  only used when DATABASE_URL is unset (local SQLite fallback)
RUN adduser --system --group --no-create-home app \
    && mkdir -p uploads/incoming uploads/processed uploads/failed uploads/archive database \
    && chown -R app:app /app

USER app

EXPOSE 8000

# /health answers GET and HEAD and never touches the database, so it stays a
# true liveness signal rather than a database check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# ONE worker on purpose: the scheduler (APScheduler) and the in-memory
# debounce queues for allocation/consolidated-workbook sends live in the
# process. A second worker would run every daily job twice.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
