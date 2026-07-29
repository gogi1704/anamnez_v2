FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY static ./static
COPY scripts ./scripts
COPY index.html dashboard.html manager.html run.py ./

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data /app/logs /app/backups \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

CMD ["python", "-u", "run.py"]
