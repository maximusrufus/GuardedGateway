FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY guardedgateway /app/guardedgateway

RUN pip install --no-cache-dir .

EXPOSE 8000
VOLUME ["/app/data"]

CMD ["uvicorn", "guardedgateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
