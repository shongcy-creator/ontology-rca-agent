FROM node:20-alpine

# ── 故障注入所需的容器内工具（仅模拟环境使用；生产镜像不应包含）─────────
#   iproute2  -> tc netem 网络延迟/丢包注入
#   stress-ng -> CPU / 内存 / IO 资源耗尽压力
#   procps    -> ps/top，便于压力期间观察进程
#   curl      -> 健康检查与端到端探针
RUN apk add --no-cache iproute2 stress-ng procps curl bash

WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY server.js ./
ENV APP_NAME=payment-app \
    APP_VERSION=1.8.3 \
    PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD wget -qO- http://127.0.0.1:8080/health || exit 1
CMD ["node", "server.js"]
