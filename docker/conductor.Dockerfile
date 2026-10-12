FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY dndmusic dndmusic
RUN pip install --no-cache-dir .
COPY starter starter
ENV DATA_DIR=/data PORT=8000 STARTER_DIR=/app/starter
VOLUME /data
CMD ["python", "-m", "dndmusic"]
