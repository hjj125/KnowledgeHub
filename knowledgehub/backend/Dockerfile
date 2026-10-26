# 后端应用镜像：API 与三个 Worker 共用一个镜像（不同 command）。
# 构建上下文 = 仓库根（docker compose 默认）：backend/ 与 workers/ 一并拷入。
# python:3.12-slim
FROM m.daocloud.io/docker.io/library/python:3.12-slim

WORKDIR /app

# 先装依赖以利用层缓存
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
    -r /app/backend/requirements.txt

# 应用代码：backend/app + workers（workers/ 导入 app/，需两个包都在 sys.path）
COPY backend/app /app/backend/app
COPY workers /app/workers

ENV PYTHONPATH="/app:/app/backend" \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend

# 默认启动 API；Worker 容器以 command 覆盖为 -m workers.<module>
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
