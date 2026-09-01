FROM python:3.11-slim

WORKDIR /app

# 依赖先行，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY send_test_trace.py .

ENV PYTHONUNBUFFERED=1 \
    STORAGE_BACKEND=es \
    HTTP_PORT=8000 \
    GRPC_PORT=4317

EXPOSE 8000 4317

# 用 shell 形式以便读取 HTTP_PORT（原实现硬编码 8000，导致该配置项形同虚设）
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${HTTP_PORT:-8000}"]
