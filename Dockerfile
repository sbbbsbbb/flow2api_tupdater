# Flow2API Token Updater v2.0 - 多 Profile 管理
# Docker + noVNC + Playwright

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99
ENV VNC_PASSWORD=flow2api
ENV NOVNC_PORT=6080
ENV VNC_PORT=5900
ENV RESOLUTION=1280x720x24

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    # VNC 和 noVNC
    x11vnc \
    xvfb \
    fluxbox \
    novnc \
    websockify \
    # Chromium 依赖
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libxshmfence1 \
    libglu1-mesa \
    # 字体
    fonts-liberation \
    fonts-noto-cjk \
    fonts-unifont \
    fonts-dejavu-core \
    # 工具
    supervisor \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium
RUN playwright install chromium

# 复制应用代码
COPY token_updater/ /app/token_updater/
COPY entrypoint.sh /app/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 创建目录
RUN mkdir -p /app/profiles /app/logs /app/data

# 修复行尾符 (CRLF -> LF) 并设置权限
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# 端口
EXPOSE 6080 5900 8002

# 持久化
VOLUME ["/app/profiles", "/app/logs", "/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
