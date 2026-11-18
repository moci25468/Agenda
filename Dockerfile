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
# --proxy-headers：nginx 反代后 request.url.scheme 才能是 https，会话 Cookie 的 Secure 才生效。
# 信任网段必须是「宿主→容器」的实际来源：容器内看到的 nginx 来源是 docker 网关（如 10.255.1.1），
# 只写 127.0.0.1 是不生效的；这里限定私有网段，公网地址无法伪造 X-Forwarded-Proto。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,::1,10.255.0.0/16,172.16.0.0/12"]
