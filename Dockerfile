FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh /usr/local/bin/sagasmith-entrypoint
RUN uv pip install --system .
RUN mkdir -p /srv/sagasmith/exchange && \
    chown -R 10001:10001 /srv/sagasmith && \
    chmod 755 /usr/local/bin/sagasmith-entrypoint

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/sagasmith-entrypoint"]
CMD ["sagasmith-service"]
