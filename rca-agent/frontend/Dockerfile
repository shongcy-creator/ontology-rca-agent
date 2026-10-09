# =============================================================================
# RCA Agent 前端 — 多阶段构建
#   Stage 1: node:20-alpine 构建 React 产物
#   Stage 2: alpine + nginx 提供服务
# =============================================================================

# ── Stage 1: Build React app ────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

# 依赖层（利用缓存）
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# 源码 + 构建
COPY . .
RUN npm run build

# ── Stage 2: Serve with nginx ───────────────────────────────────────────
FROM alpine:latest

RUN apk add --no-cache nginx curl \
    && mkdir -p /run/nginx /usr/share/nginx/html

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/http.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1/ >/dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]
