FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    TZ=Europe/London

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY volvo_mqtt ./volvo_mqtt

# Run as the same uid as the pi user so files in the bind mount stay editable
RUN useradd -u 1000 -m app
USER app

EXPOSE 8084
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.getenv('WEB_PORT','8084'), timeout=4)" || exit 1

CMD ["python", "-m", "volvo_mqtt"]
