FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 1) System deps (rozszerzone o biblioteki dla PySide6/Qt)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libegl1 \
    libgles2 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcb-render0 \
    libxcb-shm0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-render-util0 \
    libxcb-xkb1 \
    libxext6 \
    libxrender1 \
    libsm6 \
    libice6 \
    libfontconfig1 \
    libdbus-1-3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# 2) Upewnij się, że FastAPI nasłuchuje na wszystkich interfejsach
EXPOSE 8000
CMD ["uvicorn", "webapp.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
