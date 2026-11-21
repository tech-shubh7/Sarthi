# Adapted from JobRadar (MIT), Dockerfile @ fdd739b. Copyright (c) ThePlator. Modifications (c) Sarthi contributors:
# Python 3.12, Chromium for filling application forms, app files read-only, only /app/data writable.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Dependencies first (cached while only the code changes), then Sarthi itself.
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY licenses ./licenses
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

# Chromium and its system libraries. They include fonts-liberation, which the resume template uses
# (without it Typst falls back to a serif font).
RUN /app/.venv/bin/playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

# A non-root user that can write only its data folder; the app files stay owned by root.
RUN useradd --create-home --uid 1000 sarthi && mkdir -p /app/data && chown sarthi:sarthi /app/data
USER sarthi

ENV PATH="/app/.venv/bin:$PATH" \
    SARTHI_DATA_DIR=/app/data
EXPOSE 8765
ENTRYPOINT ["sarthi"]
CMD ["run"]
