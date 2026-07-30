FROM python:3.12-slim

WORKDIR /app

# 更换 Debian 国内源（清华）
RUN echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian trixie main" > /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian trixie-updates main" >> /etc/apt/sources.list && \
    echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security trixie-security main" >> /etc/apt/sources.list && \
    rm -f /etc/apt/sources.list.d/debian.sources

# 安装系统依赖 (qrcode[pil] 需要 libjpeg, ffmpeg 用于 silk→mp3 转换, gcc 用于编译 pilk 的 C 扩展)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg-dev zlib1g-dev ffmpeg gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（使用清华源）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

COPY . .

RUN mkdir -p /data

ENV DATA_DIR=/data
ENV ADMIN_PASSWORD=admin
ENV PORT=8080
ENV HOST=0.0.0.0

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/', timeout=3)" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
