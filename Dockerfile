# Lifesaver MCP server — single image, serves the MCP endpoint over
# streamable-http. Deploy exactly one instance (LifeSaver allows one session
# per user; see DEPLOY.md).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=streamable-http \
    PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY lifesaver/ ./lifesaver/
COPY mcp_server/ ./mcp_server/

RUN chown -R 1001:1001 /app
USER 1001

EXPOSE 8080

# honours $PORT (Cloud Run sets it); MCP at /mcp, health at /health
CMD ["python", "-m", "mcp_server.server"]
