FROM python:3.10-slim

WORKDIR /app
COPY . /app

# Install Poppler + OpenCV native deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      poppler-utils \
      libgl1-mesa-glx \
      libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Install Python requirements
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5021
CMD ["python", "app.py"]

