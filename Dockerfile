FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LAUNCH_DATA_DIR=/app/data \
    LAUNCH_OUT_DIR=/app/out \
    LAUNCH_LOG_DIR=/app/logs \
    LAUNCH_PORT=8000
WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/out /app/logs
EXPOSE 8000
CMD ["python", "docker_entrypoint.py"]
