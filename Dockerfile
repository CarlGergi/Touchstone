FROM python:3.12-slim-bookworm

RUN pip install --no-cache-dir uv==0.11.14

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
VOLUME /data

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "touchstone.recorder", "healthcheck", "--data-dir", "/data"]

CMD ["python", "-m", "touchstone.recorder", "run", "--data-dir", "/data"]
