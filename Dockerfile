# TradeBOT — Polymarket paper-trading agent + dashboard.
# Single image; runs the web server which also runs the agent worker in-process
# (TRADEBOT_STANDALONE=1). SQLite lives on a mounted Fly volume at /data.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Runtime config (overridable by fly.toml [env] / secrets).
ENV PYTHONUNBUFFERED=1 \
    TRADEBOT_DB_PATH=/data/portfolio.db \
    TRADEBOT_STANDALONE=1 \
    TRADEBOT_START_BALANCE=200 \
    PORT=8080

# The volume mount point for the SQLite database (persists across deploys).
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "backend/server.py"]
