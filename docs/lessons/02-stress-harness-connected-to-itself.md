[← 目录](README.md)

# 02 · mysql 压测 842/842 全失败：子进程把 127.0.0.1 当成了数据库

**Rule of thumb:** when A launches B to talk to C, verify that C looks the same **from B's
point of view**. In containers, `127.0.0.1` means "whoever is running".

## 症状

从控制台发起一次 mysql 模式压测：

```json
"requests":   {"total": 842, "success": 0, "error": 842},
"status_codes": {"no-response": 842},
"error_kinds":  {"mysql_connect_error": 842},
"latency_ms":   {"min": 0.0, "p50": 0.0},
"target":       {"mysql": "appuser@127.0.0.1:3306/creditcard"}
```

**延迟全 0 + 全是连接错误**是关键线索：不是"库被压垮了"，而是**一次都没连上**。

## 根因

控制台的压测由后端用 `subprocess` 拉起 `tools/stress_harness.py`，因此它跑在**后端容器内**；
而后端拼命令时**没有传 `--mysql-host`**，harness 用了自己的默认值 `127.0.0.1`
—— 在后端容器里，那指的是**它自己**。

后端自己连库用的是环境变量 `DB_HOST=mysql`（compose 服务别名），但这份"事实源"没有传下去。

## 修法

1. **把连接参数传下去**，并沿用后端自己那份环境变量（不再另抄一份默认值）：
   `DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME` → `--mysql-host/...`
2. **开跑前探测目标可达**（`_precheck_stress_target`）：MySQL 模式做 TCP 连接、
   HTTP 模式 `GET /health`，不可达就**当场拒绝并说明原因** ——
   一个配置错误不该长得像一次性能结论。

## 验证

```
修复前： target.mysql = appuser@127.0.0.1:3306/creditcard   842/842 失败
修复后： target.mysql = appuser@mysql:3306/creditcard        20/20 成功
```

前置探测两个方向都单独验证：不可达 → 拒绝并提示
（"容器内跑压测时地址要用 compose 服务名，`127.0.0.1` 指的是容器自己"）；可达 → 放行。
顺带确认了 `mysql` 这个名字**在宿主上解析不了、在容器里能** —— 这正是缺陷的本质。

## 附带教训

这次运行**没有造成任何实际负载**（请求全卡在连接建立阶段），但它**报告成了一次压测结果**。
如果没查错误类型只看"成功率 0%"，就会得出"数据库被打挂了"的错误结论。
