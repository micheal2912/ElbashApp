# Hebrew OCR needs the Tesseract engine; the heb/eng models ship in tessdata/.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY elbash/ elbash/
COPY web/ web/
COPY tessdata/ tessdata/

# Uploads, the user database and the session key all live on one volume.
ENV ELBASH_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 elbash \
 && mkdir -p /data && chown -R elbash:elbash /data /app
USER elbash

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# Exactly one worker: jobs are tracked in memory, so a second worker would not
# see jobs created by the first. Concurrency comes from the thread/process
# pools inside a job, which already use every core.
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
