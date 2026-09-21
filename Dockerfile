# ─────────────────────────────────────────────────────────────────────
# dra_harness API
#
# The agent runs MODEL-GENERATED CODE via tools.python_execute and
# tools.bash_execute. The container IS the sandbox. Do not run this image
# with --privileged, do not mount host paths read-write, and do not run it
# on a box that holds anything you would mind an LLM reading.
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DRA_RUN_ROOT=/data/runs

# Build deps for pdf/image wheels; dropped after install.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-api.txt .
RUN pip install --no-cache-dir -r requirements-api.txt \
    && apt-get purge -y build-essential && apt-get autoremove -y

# Only the package the API needs. The rest of the repo is not copied.
COPY dra_harness/ ./dra_harness/

# Non-root. Model-generated code executes as this user.
RUN useradd --create-home --uid 10001 dra \
    && mkdir -p /data/runs /data/inputs \
    && chown -R dra:dra /app /data
USER dra

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# --workers 1 is REQUIRED: job state lives in this process's memory.
CMD ["uvicorn", "dra_harness.api:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
