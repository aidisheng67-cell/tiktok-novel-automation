FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NOVEL_CONFIG=/app/config/config.json \
    NOVEL_DB=/data/state.db \
    NOVEL_AUDIO_DIR=/data/audio \
    NOVEL_BACKGROUND_DIR=/data/background \
    NOVEL_OUTPUT_DIR=/data/output \
    NOVEL_ICON_DIR=/data/icons \
    NOVEL_FFMPEG=/usr/bin/ffmpeg \
    NOVEL_FFPROBE=/usr/bin/ffprobe \
    NOVEL_FONT_FILE=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

# ffmpeg：渲染与去重滤镜链；dejavu：容器内可用的中英文字体
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./
COPY console.html levers.json config.example.json ./
COPY src ./src

RUN useradd --create-home --uid 10001 novel \
    && mkdir -p /app/config /app/logs /app/work /data/audio /data/background \
                /data/output /data/icons \
    && chown -R novel:novel /app /data

USER novel

VOLUME ["/data"]

# 默认只做安全预览（不上传、不发布）。
# 真执行：docker compose run --rm app python schedule_cli.py run --limit 1
CMD ["python", "schedule_cli.py", "plan"]
