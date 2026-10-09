[← 目录](README.md)

# 01 · 三个页面全 502，而所有容器都 healthy

**Rule of thumb:** "containers are healthy" ≠ "the feature works". Every hop that involves a
lookup, a redirect or a cache needs its own end-to-end probe.

## 症状

控制台的「故障注入」「压测」「成本」三个页面全部返回 `502 Bad Gateway`。与此同时：

```
docker ps                        → rca-agent-backend   Up 5 hours (healthy)
curl 127.0.0.1:8088/api/health   → 200          ← 直连后端正常
curl 127.0.0.1:3001/api/health   → 502          ← 走 nginx 就断
docker logs rca-agent-backend    → 只有来自 172.18.0.1（宿主）的请求，
                                   没有任何来自前端容器的请求
```

最后一行是决定性的：**nginx 根本没把请求发给后端**。

## 为什么难查

所有"健康检查"都是绿的 —— 容器 healthy、后端自检 200、Prometheus 照常抓指标。
断的是**唯一没有人监控的那一跳**：浏览器 → nginx → 后端。

## 根因

```nginx
proxy_pass http://rca-agent-backend:8088;   # 字面主机名
```

nginx 对**字面主机名**只在**启动时**解析一次，并把 IP 缓存到 worker 生命周期结束。
本会话中后端容器因为改动被重建 → 换了 IP → 这条路**永久断掉**，直到有人重启前端。

## 修法

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;          # Docker 内嵌 DNS
set $backend_upstream http://rca-agent-backend:8088;
proxy_pass $backend_upstream;                     # 带变量 → 按周期重解析
```

**验证方式（关键）**：不能只"重启一下看看好了没"。要**用占位容器占住后端原来的 IP**，
再重建后端强制它换地址，然后确认前端**不重启**也能正常代理：

```
后端 172.18.0.16 → 占位容器占住 .16 → 重建后端 → 172.18.0.17
前端未重启：/api/health 200、/api/chaos/status 200、/api/agent/cost 200  ✅
```

（顺带说：第一次尝试时 Docker 复用了同一个 IP，那次等于没测到 —— 所以必须强制换地址。）

**并做成自动化**：`doctor` 新增一项「前端→后端 代理连通（nginx）」，直接模拟
"浏览器 → nginx → 后端"这条真实路径。负向测试：停掉后端 → 该项 FAIL 并给出准确提示。
