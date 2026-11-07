FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    CAMPUS_DATA_DIR=/data \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright

WORKDIR /srv/app
COPY requirements.txt .
# apt 走阿里云镜像，避免 deb.debian.org 在国内构建时超时；
# 这里不再用 || true 吞掉错误——浏览器装不上就让构建失败，而不是运行时才暴露。
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    pip install --no-cache-dir --default-timeout=120 -r requirements.txt && \
    playwright install --with-deps chromium-headless-shell && \
    rm -rf /root/.cache/ms-playwright/*.zip /var/lib/apt/lists/*

COPY app ./app
COPY static ./static

VOLUME ["/data"]
EXPOSE 8300
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300"]
