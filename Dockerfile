FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY guardedgateway /app/guardedgateway

RUN pip install --no-cache-dir .

# DB durability: GuardedGateway uses raw sqlite3 (guardedgateway/ledger.py,
# tenants.py; no SQLAlchemy in this repo), so durability is provided by
# mounting a persistent volume at the directory named by GG_DB_PATH
# (defaults to data/guardedgateway.db under the working dir) -- do that in
# production, not the container layer.
EXPOSE 8000
VOLUME ["/app/data"]

# Honor Cloud Run's PORT env var (falls back to 8000 for local `docker run`).
CMD ["sh", "-c", "uvicorn guardedgateway.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
