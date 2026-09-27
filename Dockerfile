FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FGISCS_CACHE_DIR=/app/data/cache \
    FGISCS_MCP_TRANSPORT=streamable-http \
    FGISCS_MCP_HOST=0.0.0.0 \
    FGISCS_MCP_PORT=8000 \
    FGISCS_MCP_PATH=/mcp

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data/cache \
    && groupadd --system --gid 10001 fgis \
    && useradd --system --uid 10001 --gid fgis --home-dir /app fgis \
    && chown -R fgis:fgis /app/data/cache

USER 10001:10001

EXPOSE 8000

CMD ["python", "/app/server.py"]
