[← 目录](README.md)

# 06 · OOM 计数器永远是 0：证据不能放在被观测对象自己身上

**Rule of thumb:** anything that can be destroyed along with its subject must be observed
from **outside** it. A counter a container exposes about itself dies with the container.

## 症状

场景 `app_oom_kill` 的判据含 `increase(cc_container_oom_kill_total[5m]) > 0`，
而它**永远不成立**。同一时刻：

```
注入层（docker inspect）："oom_observed": true   "limit_applied": true   ← 真的杀了
容器内计数器：            instance=…a4a4=0  …9efb=0  …404a=0            ← 三个实例全为 0
```

## 根因

`cc_container_oom_kill_total` 是**容器内**应用读 cgroup 得到的：
容器被 OOM 杀掉并重启后，**新容器从 0 开始**。Prometheus 眼里这条序列"生来是 0"，
`increase()` 永远是 0 —— **证据随着它证明的那件事一起消失了**。

（这也解释了为什么它偶尔"看起来正常"：当内核杀的是容器里的 `stress-ng`
**进程**、容器没有重启时，计数器留在原地，就看得见。**偶发通过比稳定失败更危险**，
因为它会被误认为"已经修好了"。）

## 修法：把观测点搬到容器外

新增 `rca-agent/monitoring/container/exporter.py` —— 容器外读 Docker API，
暴露容器生命周期里**单调**的量：

```
cc_container_restart_count{container="..."}   容器重启次数（注入后 +1，可 increase()）
cc_container_oom_killed{container="..."}      最近一次退出是否因 OOM
cc_container_running{container="..."}         是否在运行
```

实现上只用**标准库**（`http.client` 直接走 UNIX socket 的 Docker API），
因此镜像可以用干净的 `python:3.11-alpine` —— **不需要 Dockerfile、不需要 docker CLI**。
告警规则改为两条证据取并集，场景判据也换成容器外证据。实测：

```
container=rca-agent-payment-app-1 restart_count=1
container=rca-agent-payment-app-2 restart_count=1
container=rca-agent-payment-app-3 restart_count=0     ← 不随重启消失 ✓
```

## 为什么这条最值得记

它不是一个操作失误，而是一个**普遍的设计陷阱**：

- 观测"容器被重启"用容器内的指标 → 必然失败
- 观测"进程崩溃"用进程内的日志 → 崩溃后日志也丢了
- 观测"节点失联"用节点上的 agent → 节点挂了 agent 也挂了

凡是**被观测对象可能整体消失**的量，观测点就必须在它之外。
