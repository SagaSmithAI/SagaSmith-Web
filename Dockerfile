FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.25
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/sagasmith-entrypoint
RUN uv sync --frozen --no-dev
RUN mkdir -p /srv/sagasmith/exchange && \
    chown -R 10001:10001 /srv/sagasmith && \
    chmod 755 /usr/local/bin/sagasmith-entrypoint

USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/sagasmith-entrypoint"]
CMD ["sagasmith-service"]
