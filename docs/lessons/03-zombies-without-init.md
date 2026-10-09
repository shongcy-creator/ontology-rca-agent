[← 目录](README.md)

# 03 · 后端容器僵尸只增不减：PID 1 不负责收尸

**Rule of thumb:** a container whose PID 1 is your application does not reap orphans.
If anything in it forks (health checks, subprocesses), add `init: true`.

## 症状

`doctor` 的「容器无僵尸进程」从 OK 变成 FAIL：

```
容器无僵尸进程   FAIL   rca-agent-backend=3(curl=3)
```

## 为什么难查

容器「healthy」、接口正常、指标正常 —— **僵尸进程本身不影响功能**，
它只是缓慢泄漏 PID。真正的问题是它会污染"环境是否干净"的判断，
并在长时间运行后耗尽 PID 表。

## 根因（用 /proc 实测确认）

```
后端容器 PID 1 = python3 -m uvicorn ...     ← 不是 init
PID 64407  PPID 1  Z  [curl]                ← 被 PID 1 收养，永不回收
```

链条：后端镜像的 `HEALTHCHECK` 每 30s 跑一次 `curl`；这些短期进程退出时，
若有残留子进程，就会被**收养给 PID 1**；而 PID 1 是 uvicorn ——
**它不会 `wait()` 那些不是它自己 fork 的进程** → 僵尸累积。

MySQL 与应用副本没这个问题，因为 compose 里它们有 `init: true`（PID 1 = tini，会收尸）；
`rca-agent-backend` / `rca-agent-frontend` / `app-gateway` **漏了**。

## 修法

给这三个服务补 `init: true`，重建即可：

```
HostConfig.Init   false → true
容器无僵尸进程     FAIL → OK
```

## 附带教训（重建类操作）

第一次重建时我把服务名写成了 `cc-app-gateway`（那是 `container_name`，
**服务名其实是 `app-gateway`**）→ `docker compose up` 遇到未知服务名会
**整体报错、什么都不重建**，于是僵尸数没变，一度看起来像"加了 init 也没用"。

> **重建类操作必须核对 `docker compose config --services` 给出的真实服务名**，
> 不能拿 `docker ps` 里的容器名去凑。
