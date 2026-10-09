# Linux Docker 迁移适配

> 目标：本项目从 **Windows Docker Desktop** 迁移到 **Linux Docker**，行为等价。
>
> **本文的证据边界**：所有"已核实"项都是我在 Windows 环境上对**代码与配置**做的静态核查与实际改动验证
> （`py_compile`、`docker compose config`、`doctor` PASS）；**我从未在 Linux 上跑过本项目**，
> 因此第四节"必须在 Linux 上复测"是**未验证项**，不是"应该没问题"。

---

## 一、结论速览

| 类别 | 数量 | 状态 |
|---|---|---|
| Linux 阻断项（代码/配置） | 5 | ✅ 已修（见第二节） |
| 环境侧必做（不用改代码） | 3 | ⬜ 迁移时执行（第三节） |
| 必须在 Linux 上复测/标定 | 4 | ⬜ 未验证（第四节） |
| 已知技术疑点 | 2 | ⚠️ cgroup 版本、`tc netem` 内核模块（第五节） |

---

## 二、已修的 5 处（Windows → Linux 阻断项）

### 1. 硬编码 Windows 盘符 —— Linux 上直接崩

| 文件 | 原 | 现 |
|---|---|---|
| `tools/rca_report.py:16` | `Path('D:/05_code/credit-card-sys-ops/tools')` | `Path(__file__).resolve().parent` |
| `tools/test_rca_engine.py:3-4` | `sys.path.insert(0, "D:/05_code/.../rca-agent")` + `PYTHONPATH` | 补最小前导 `_ROOT = _Path(__file__).resolve().parent.parent`，改用 `str(_ROOT / "rca-agent")` |

### 2. Windows 专有调用

| 文件 | 原 | 现 |
|---|---|---|
| `tools/restart_and_test.py:5` | `["taskkill", "/F", "/IM", "python.exe"]` | 同一表达式 + `if os.name == "nt" else ["pkill", "-f", "python"]`（并自动补 `import os`） |

> 这是全仓**唯一**一处 Windows 专有调用（扫描 `powershell|.ps1|Start-Process|taskkill|os.startfile|chcp` 的结果）。
> Windows 上行为不变。

### 3. SELinux 标签 —— RHEL / CentOS / Fedora / openEuler 上必炸

`rca-agent/docker-compose.yml` 的 **12 处 bind mount 全部加了 `:z`**（含只读挂载写成 `:ro,z`）：

```
./gateway/nginx.conf、./monitoring/mysql/init、./monitoring/prometheus/{prometheus,alerts}.yml、
./monitoring/blackbox/blackbox.yml、./monitoring/grafana/{provisioning,dashboards}、
./backend、../.evoontology、../vendor/EvoOntology
```

**只跳过 `/var/run/docker.sock`**（socket 不需要文件标签）。
`:z` 在非 SELinux 宿主（Debian/Ubuntu）与 Windows Docker Desktop 上都是 **no-op**，
因此这一改动是"加兼容"而非"换行为"。

> 踩坑记录：第一版脚本的跳过清单里**误加了 `monitoring/prometheus/prometheus.yml`**，
> 导致它漏加 `:z`。是靠"逐行打印改动后的挂载列表"发现的 —— 迁移类批量替换**必须回读结果**，
> 不能只看"处理了 N 处"。

### 4. 换行符约定（`.gitattributes`，新增）

```
* text=auto
*.sh / *.sql / *.py / *.js / *.ts / *.tsx / *.json / *.yml / *.yaml / *.conf / *.md / Dockerfile → eol=lf
*.png / *.jpg / *.gif → binary
```

**实测当前仓库所有 `.sh`/`.sql` 的 CR 字节为 0**，所以这不是在修 bug，而是把事实固化成约定 ——
防止以后有人从 Windows 提交 CRLF，让挂进容器的脚本出现 `#!/bin/sh^M` 这类只在 Linux 上炸的问题。

### 5. 已核实"本来就没事"的项（无需改动）

| 检查项 | 结果 |
|---|---|
| `host.docker.internal`（Docker Desktop 专有，Linux 默认**不存在**） | **0 处引用** → 不需要 `extra_hosts` |
| 挂进容器的 `.sh` / `.sql` 换行符 | CR 字节 **0** |
| Docker socket | 已挂载（`/var/run/docker.sock:ro`），控制台操作容器的前提在 Linux 上同样成立 |
| 服务间寻址 | 全部走 compose 服务名 DNS，无跨容器 `localhost` 硬编码 |
| 容器初始化 | 后端/前端/gateway 已补 `init: true`（见手册 §11.40），Linux 上同样生效 |

---

## 三、环境侧必做（不用改代码）

```bash
# 1) 起服务
docker compose -f rca-agent/docker-compose.yml up -d

# 2) **引导集群**：建 schema + 把副本指向主库并启动复制。
#    这一步**不在 compose 里**（本地环境往往早就跑过，容易漏）。漏了的表现是：
#    doctor 的「读路径」报 Access denied（副本上根本没有 appuser）、
#    两条「replication」FAIL（SHOW REPLICA STATUS 为空）—— 因为副本是空实例。
python tools/cluster_bootstrap.py

# 3) 重建数据集 —— 命名卷按 project 名创建，新宿主不会带走 MySQL 数据。
#    不 seed 的话 t_txn 是空的，doctor 的「测试体量 t_txn」与依赖它的场景全部失真。
python tools/fault_injector.py seed

# 4) 环境体检（期望 18 项 PASS）
python tools/fault_injector.py doctor

# 5) 文件属主：容器（root）写 .chaos / .evoontology，宿主用户改不动
sudo chown -R "$USER":"$(id -gn)" .chaos .evoontology
```

**注意**：`docker compose` 必须是 **v2**（v1 的 `docker-compose` 不认 `init:`、`cpus:` 等键）。

---

## 四、必须在 Linux 上复测（**未验证**，别跳过）

现有**全部阈值与基线**是在 Windows Docker Desktop（WSL2 受限虚拟机）上标定的。
换到真实 Linux 服务器（核数/内存/磁盘都不同）后，绝对阈值不一定仍成立。
按顺序跑，**用结果决定要不要调**：

```bash
python tools/fault_injector.py doctor                                    # 前提
python tools/stress_harness.py --mode http --concurrency 32 --duration 45 # 重取延迟/吞吐基线
python tools/fault_injector.py verify                                     # 21 场景
python tools/alert_coverage_check.py                                      # 告警覆盖
```

**怎么看结果**（这几项都是本会话新加的口径）：

- `verify`：重点看 **「注入期全部声明信号成立 N/21」**。现在是 21/21（判据窗口 [30s] + hold 240s）。
  若 Linux 上变差，**先怀疑阈值/窗口重标定，不要直接改成"通过"** —— 判据的时间窗口必须短于验证等待，
  这是 §11.40 记下的规则。
- `alert_coverage`：看 `declared_but_silent`。阈值不适配会在这里暴露。
- `stress_baseline`：只做基线，不用"通过/失败"判断；把新基线与旧值（Linux 上预期更快）都记进报告。

**阈值变更纪律**：一律走 `tools/evolve_ontology_cluster.py` 的版本化轮次 + 成对评估闸门，
不允许为了"跑绿"直接手改 `catalog.py` 的数字。

---

## 五、两个已知技术疑点（未验证）

### 1. cgroup 版本差异（Linux 特有）

应用指标直接读 cgroup。`cc_container_oom_kill_total` 已实现 **v2 优先 + v1 回退**
（`memory.events` → `memory.failcnt`）；但 **CPU 节流类指标**尚未补 v1 分支：

| cgroup | 节流字段 |
|---|---|
| v2 | `cpu.stat` → `throttled_usec` |
| v1 | `cpu/cpu.stat` → `throttled_time` + `nr_throttled` |

老发行版（CentOS 7 / Ubuntu 18.04）默认 cgroup v1。**到 Linux 后先看**
`/metrics` 里 `cc_container_cpu_throttled_seconds_total` 是否非零，再决定要不要补分支：

```bash
stat -fc %T /sys/fs/cgroup     # cgroup2fs = v2；tmpfs = v1
```

同理 `docker update --memory-swap` 在 cgroup v2 下 swap 语义不同，`app_oom_kill` 场景可能只靠 `--memory` 生效。

### 2. `tc netem` 依赖宿主内核模块

网络延迟/丢包场景需要宿主有 `sch_netem` + 容器有 `NET_ADMIN`：

```bash
sudo modprobe sch_netem
```

缺模块时 `tc qdisc add ... netem` 报 *Operation not supported* —— 代码会**明确报错**（不会静默通过 ✅），
届时按错误信息处理即可。

---

## 六、迁移验收口径

"迁移成功"不是"容器起来了"，而是：

| 验收项 | 口径 |
|---|---|
| 环境 | `doctor` **18/18 PASS**（含"前端→后端代理连通"与"容器无僵尸进程"两项） |
| 数据集 | `t_txn` 行数与预期一致（Windows 上为 483 万） |
| 场景 | `fault_verify` 21/21 通过，且**注入期全部声明信号成立 ≥ 20/21** |
| 告警 | `alert_coverage` 覆盖率 ≥ 20/21 |
| 诊断 | `tools/cluster_rca_verify.py` 严格命中与 Windows 基线同量级（不要求逐点相同，但不得显著退化） |
| 复现性 | `run_all_verification.py` 的记录里 `code fingerprint` 与 Windows 一致（同代码），仅环境指纹不同 |

---

## 七、迁移操作清单（按顺序）

```
□ 1. 代码侧：本节第二部分的 5 项已在仓库中（无需再改）
□ 2. Linux 宿主：装 Docker + compose v2；stat -fc %T /sys/fs/cgroup 记下 cgroup 版本
□ 3. 拷贝项目（保持目录结构）
□ 4. docker compose -f rca-agent/docker-compose.yml up -d
□ 5. python tools/fault_injector.py seed
□ 6. python tools/fault_injector.py doctor          → 期望 18/18 PASS
□ 7. sudo chown -R $USER .chaos .evoontology
□ 8. 第四节四项复测 → 记录新基线；阈值不适配则走版本化闸门调整
□ 9. 第五节两个疑点按实际现象处理（cgroup v1 分支 / sch_netem）
```

---

## 八、与"完整泛化"的边界

本文只解决 **同一套 compose 换宿主 OS**。
若目标是 **app 任意、部署形态任意（VM/物理机）、数据库换 PG/GaussDB**，那是另一套改造
（运行提供者抽象 + 数据库方言 + 规范化信号层 + 本体实例化），不在这份文档范围内。

---

## 附：上面这几步已收敛为一条命令

三、四节的步骤仍列出来（便于逐段排查），但**日常只需要**：

```bash
python tools/dev_up.py --build
# 等价于：起栈 → cluster_bootstrap → seed → doctor（含重试）
```

之所以收敛：`cluster_bootstrap.py` 这一步不在 compose 里，曾被漏掉三次
（CI / 本文档 / 英文 README），而漏掉时的症状是**副本空实例** ——
所有容器 healthy，只有 `doctor` 能看出来。
