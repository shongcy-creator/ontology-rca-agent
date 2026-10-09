# =============================================================================
# RCA Agent 后端 — Alpine + Python3 + docker-cli
#   docker-cli 用于环境层工具（容器列表/日志/资源/详情），通过挂载的
#   /var/run/docker.sock 以只读方式访问 Docker。
# =============================================================================
FROM alpine:latest

# ── 系统依赖：Python + docker CLI（环境取证）─────────────────────────────
RUN apk add --no-cache python3 py3-pip tzdata curl docker-cli \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && python3 -m pip install --no-cache-dir --break-system-packages --upgrade pip

WORKDIR /app

# ── Python 依赖 ──────────────────────────────────────────────────────────
COPY backend/requirements.txt ./
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt

# ── 后端代码 ─────────────────────────────────────────────────────────────
COPY backend/ ./backend/

ENV PYTHONPATH=/app \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PORT=8088

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8088/api/health || exit 1

CMD ["python3", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8088"]
