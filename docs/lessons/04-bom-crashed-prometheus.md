[← 目录](README.md)

# 04 · 一个 BOM 把 Prometheus 打崩 10 次

**Rule of thumb:** writing config is not a text edit — validate it, and read back what you
actually wrote. `Set-Content -Encoding UTF8` in PowerShell writes a BOM; that is not a safe
default for machine-parsed files.

## 症状

改完 `alerts.yml` 后，Prometheus 进入崩溃循环：

```
cc-prometheus   Restarting (1)   restart count = 10
logs: level=error msg="Failed to apply configuration"
      err="/etc/prometheus/alerts.yml: yaml: line 24: did not find expected ..."
```

## 根因

用 PowerShell 的 `Set-Content -Encoding UTF8` 写入 → **写了 UTF-8 BOM**（首 3 字节 239,187,191），
同时该次替换破坏了 YAML 结构。Prometheus 因此规则加载失败、不断重启。

（注意：BOM 与结构破坏是两个问题；即使 BOM 被容忍，YAML 坏了照样崩。）

## 修法（两步都值得复用）

1. **从索引恢复**：这次改动发生在 `git add` **之后** → 索引里还是干净版本，
   `git checkout -- <file>` 一步还原。（教训：改文件前先确认提交/暂存状态，
   它是最省事的"撤销"来源。）
2. **无 BOM 改写 + 回读校验**：

```powershell
[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($false)))   # 无 BOM
$b = [IO.File]::ReadAllBytes($p); $b[0],$b[1],$b[2]                       # 回读首字节：35,32,61 ✓
python -c "import yaml;yaml.safe_load(open('alerts.yml'))"                # 解析校验
docker compose config --quiet                                             # 结构校验
```

## 可迁移的规则

**"命令执行成功"不等于"文件是对的"。** 对机器解析的文件（YAML/JSON/TOML/INI），
写完必须：① 用不会加 BOM 的方式写；② 回读校验；③ 用真正的解析器再解一遍。
这与本项目另一条纪律同源：**别信退出码，信断言**。
