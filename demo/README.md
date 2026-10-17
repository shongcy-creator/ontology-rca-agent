# demo — 单机演示栈

单实例简化栈（1 app + 1 MySQL + Prometheus/Grafana/mysqld-exporter），
用于快速理解指标面；完整集群版（3 副本 + 主从 + 网关 + 黑盒探针）见
[rca-agent/docker-compose.yml](../rca-agent/docker-compose.yml)。

## 运行

```bash
cd demo
docker compose up -d
# 可选：对外暴露（同时必须改掉所有口令）
CC_BIND=0.0.0.0 docker compose up -d
```

| 服务 | 地址（默认仅 127.0.0.1） |
|---|---|
| payment-app | http://localhost:8080 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (admin / admin123) |
| MySQL | localhost:3306 (appuser / apppass) |

## ⚠️ 安全口径

- 本栈是**隔离演示环境**：所有端口默认只绑 `127.0.0.1`（`${CC_BIND:-127.0.0.1}`）。
- 文件里的 `apppass` / `rootpass` / `admin123` 是**演示默认值**，只允许跑在本机。
  对外暴露前必须：① 设置 `CC_BIND` 时同时替换全部口令；② Grafana 走
  `GRAFANA_ADMIN_PASSWORD` 覆盖。
- 不要把本目录当生产参考抄。生产口径（回环绑定、注入令牌、只读账号）见
  [rca-agent/docker-compose.yml](../rca-agent/docker-compose.yml) 头部注释与
  [SECURITY.md](../SECURITY.md)。
