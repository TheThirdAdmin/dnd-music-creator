FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

ARG COMFYUI_VERSION=v0.39.2
RUN git clone --depth 1 --branch ${COMFYUI_VERSION} https://github.com/comfyanonymous/ComfyUI.git /opt/comfyui \
 && rm -rf /opt/comfyui/.git

ARG TORCH_INDEX=https://download.pytorch.org/whl/cu130
RUN pip install --no-cache-dir torch==2.14.1 torchvision==0.29.1 --index-url ${TORCH_INDEX} \
 && pip install --no-cache-dir -r /opt/comfyui/requirements.txt

WORKDIR /app
COPY generator/requirements.txt generator/requirements.txt
RUN pip install --no-cache-dir -r generator/requirements.txt
COPY generator generator
COPY NOTICE NOTICE
RUN mkdir -p /models && chmod 1777 /models
ENV MODEL_DIR=/models PORT=8001 COMFY_DIR=/opt/comfyui WORK_DIR=/tmp/dndgen \
    HOME=/tmp USER=dndmusic LOGNAME=dndmusic PYTHONUNBUFFERED=1
VOLUME /models
EXPOSE 8001
CMD ["python", "-m", "generator"]
