FROM python:3.11-slim

WORKDIR /app

# 基础工具（curl 用于健康检查；ca-certificates 用于 HTTPS 数据源）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用镜像层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

EXPOSE 8000

# 以 uvicorn 生产方式启动；可通过 -e PORT=xxxx 调整端口
CMD ["sh", "-c", "uvicorn web_app:app --host 0.0.0.0 --port ${PORT:-8000}"]
