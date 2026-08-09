# Build from the repository root:  docker build -f backend/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/
COPY frontend/ /frontend/

# Run as a non-root account.  The process parses attacker-supplied PDFs and
# images (pypdf, Pillow, img2pdf) and talks to two external APIs, so a memory
# bug in any of them should not land on uid 0 with the OCR/LLM keys, the DB
# credentials and the session secret all in the environment.  storage/ is
# chowned because it is the one path the app writes at runtime; declare it a
# VOLUME so uploads and the SQLite file survive `docker compose build`, which
# otherwise discarded every stored customs document on each rebuild.
RUN useradd --system --create-home --uid 10001 easycustoms \
    && mkdir -p /app/storage \
    && chown -R easycustoms:easycustoms /app/storage
VOLUME ["/app/storage"]
USER easycustoms

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
