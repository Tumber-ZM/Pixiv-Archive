FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PA_HOST=0.0.0.0
ENV PA_PORT=6814
ENV PA_DATA_DIR=/app/data
ENV IMAGE_SOURCE_DIR=/app/data/images
ENV AUTO_WATCH_ENABLED=1
ENV AUTO_WATCH_DEBOUNCE_SECONDS=30
ENV AUTO_WATCH_POLLING=0
ENV PA_LOG_LEVEL=INFO

ARG PIP_INDEX_URL=https://pypi.org/simple

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/data/images

EXPOSE 6814

CMD ["python", "-u", "run.py", "--lan"]
