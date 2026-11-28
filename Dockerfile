# syntax=docker/dockerfile:1
# Python 3.12, Chromium for filling application forms, app files read-only, only /app/data writable.
# Slow, rarely changing layers first (system packages, Chromium, libraries), Sarthi's own code last, so a code
# change rebuilds in about a minute instead of reinstalling Chromium.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# libatomic1: Playwright's bundled Node needs it and the slim image no longer ships it.
# Logging in to a job site needs a visible browser. There is no desktop in a container, so a login runs on
# a virtual screen (Xvfb) shown as a web page at http://127.0.0.1:6080 (x11vnc + noVNC), only while it
# lasts (sarthi.apply.screen).
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 xvfb x11vnc novnc websockify \
    && rm -rf /var/lib/apt/lists/*

# The libraries (cached while only the code changes).
COPY pyproject.toml uv.lock ./
# uv's download cache stays out of the image (it was hundreds of MB in a layer, never used at run time) and is
# kept between builds instead, so a rebuild downloads nothing again.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

# Chromium and its system libraries. They include fonts-liberation, which the resume template uses
# (without it Typst falls back to a serif font).
RUN /app/.venv/bin/playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

# DuckDB's httpfs extension (the job catalog reads a remote Parquet file), installed now: at run time the
# home folder is a no-exec tmpfs, where a downloaded extension cannot be loaded.
ENV SARTHI_DUCKDB_EXTENSIONS=/app/duckdb-extensions
RUN /app/.venv/bin/python -c "import duckdb; duckdb.connect(config={'extension_directory': '/app/duckdb-extensions'}).install_extension('httpfs')"

# Sarthi itself.
COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

# A non-root user that can write only its data folder; the app files stay owned by root.
RUN useradd --create-home --uid 1000 sarthi && mkdir -p /app/data && chown sarthi:sarthi /app/data
USER sarthi

ENV PATH="/app/.venv/bin:$PATH" \
    SARTHI_DATA_DIR=/app/data
EXPOSE 8765 6080
ENTRYPOINT ["sarthi"]
CMD ["run"]
