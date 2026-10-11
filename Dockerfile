FROM python:3.12-slim-bookworm

# WeasyPrint libs + Tesseract 5.5 from notesalexp (Debian Bookworm apt is 5.3.x).
# Correct channel is tesseract-ocr5/bookworm — not tesseract-ocr/bookworm.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    apt-transport-https \
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
    && echo "deb [signed-by=/etc/apt/keyrings/notesalexp.gpg] https://notesalexp.org/tesseract-ocr5/bookworm/ bookworm main" \
       > /etc/apt/sources.list.d/notesalexp.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && tesseract --version | head -n 1 | grep -E "tesseract 5\.[5-9]|tesseract [6-9]" \
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
