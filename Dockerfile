FROM python:3.12-slim-bookworm

# WeasyPrint libs + newer Tesseract from notesalexp (Bookworm apt is stuck on 5.3.x).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu-core \
    libglib2.0-0 \
    libgomp1 \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://notesalexp.org/debian/alexp_key.asc \
       | gpg --dearmor -o /etc/apt/keyrings/notesalexp.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/notesalexp.gpg] https://notesalexp.org/tesseract-ocr/bookworm/ bookworm main" \
       > /etc/apt/sources.list.d/notesalexp.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DATA_DIR=/data
ENV PORT=8000
EXPOSE 8000

VOLUME ["/data"]

CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT} -w ${WEB_CONCURRENCY:-2}"]
