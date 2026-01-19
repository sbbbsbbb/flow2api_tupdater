# Flow2API Token Updater v3.0 - 轻量版
# 无 VNC，通过 Cookie 导入登录

FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# 安装 Chromium 依赖 (精简版)
RUN apt-get update && apt-get install -y --no-install-recommends \
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
    fonts-liberation \
    fonts-noto-cjk \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright Chromium
RUN playwright install chromium

# 复制应用代码
COPY token_updater/ /app/token_updater/
COPY entrypoint.sh /app/

# 创建目录
RUN mkdir -p /app/profiles /app/logs /app/data

# 修复行尾符并设置权限
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# 端口
EXPOSE 8002

# 持久化
VOLUME ["/app/profiles", "/app/logs", "/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
