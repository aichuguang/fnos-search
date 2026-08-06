ARG PYTHON_IMAGE=python:3.11-slim
ARG RCLONE_IMAGE=rclone/rclone:1.70.3
ARG APP_UID=10001
ARG APP_GID=10001

FROM ${RCLONE_IMAGE} AS rclone

FROM ${PYTHON_IMAGE}

ARG APP_UID=10001
ARG APP_GID=10001
ARG APP_VERSION=dev
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="FNOS Media Import" \
    org.opencontainers.image.description="Media search, import and library organization service for FNOS" \
    org.opencontainers.image.source="https://github.com/aichuguang/fnos-search" \
    org.opencontainers.image.version="${APP_VERSION}" \
    org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/app

WORKDIR /app

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_EXTRA_INDEX_URLS="https://mirrors.aliyun.com/pypi/simple https://mirrors.cloud.tencent.com/pypi/simple https://pypi.tuna.tsinghua.edu.cn/simple"
RUN set -eux; \
    install_deps() { \
        index_url="$1"; \
        echo "Installing Python dependencies from ${index_url}"; \
        pip install --no-cache-dir --timeout 120 --retries 8 --index-url "$index_url" -r requirements.txt; \
    }; \
    if install_deps "$PIP_INDEX_URL"; then \
        exit 0; \
    fi; \
    for index_url in $PIP_EXTRA_INDEX_URLS; do \
        if [ "$index_url" = "$PIP_INDEX_URL" ]; then \
            continue; \
        fi; \
        if install_deps "$index_url"; then \
            exit 0; \
        fi; \
    done; \
    exit 1

RUN addgroup --gid "${APP_GID}" app \
    && adduser --uid "${APP_UID}" --disabled-password --gecos "" --ingroup app --home /home/app app \
    && mkdir -p /app/config /app/data /app/logs /config/rclone /cache /temp /home/app \
    && chown -R app:app /app /config/rclone /cache /temp /home/app

COPY --chown=app:app . .

EXPOSE 5251
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5251/readyz', timeout=5).read()"

ENTRYPOINT ["python", "/app/scripts/container_entrypoint.py"]
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--bind", "0.0.0.0:5251", "--timeout", "180", "--graceful-timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
