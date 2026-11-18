FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git git-lfs openssh-client ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git lfs install --system
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE.txt ./
COPY gitmirror.py ./
RUN pip install --no-cache-dir . \
 && useradd --create-home --uid 10001 gitmirror \
 && mkdir -p /data /config \
 && chown -R gitmirror:gitmirror /data /config
USER gitmirror
ENV HOME=/home/gitmirror PYTHONUNBUFFERED=1
VOLUME ["/data"]
ENTRYPOINT ["gitmirror"]
CMD ["--config", "/config/config.yml", "run"]
