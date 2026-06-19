FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    libmagic1 \
    tesseract-ocr \
    antiword \
    unrtf \
    poppler-utils \
    subversion \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

ENV STANDARDS_DB_PATH=/app/standards_index.sqlite \
    MAX_UPLOAD_MB=50 \
    SVN_BIN=/usr/bin/svn

EXPOSE 8002

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8002", "--proxy-headers"]
