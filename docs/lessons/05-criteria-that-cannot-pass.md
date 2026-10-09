[← 目录](README.md)

# 05 · 三次"判据在结构上不可能通过"

**Rule of thumb:** a check that can never pass is as useless as one that can never fail.
When a check fails, first ask **"could it ever have succeeded?"**

三次都发生在同一件事上：**验证一个系统是否符合预期**。三次的形态不同，
但都是"判据设计缺陷"，而不是被测系统的问题。

## 形态一：窗口长于等待（4 个场景）

```promql
rate(mysql_global_status_created_tmp_disk_tables[2m]) > 0.05
```

验证只等 ~75s，而 2 分钟窗口**根本没铺满** → 永远为假，被判"故障没复现"。
更早还有一次是"证据出现得太晚"：MySQL 副本**冷启动 167 秒**，而等待预算是 120s ——
窗口到期时副本还没回来，且期间 Prometheus 仍返回**陈旧的高 uptime**。

**修法**：窗口必须短于等待；并优先选**持久可观测**的证据形态
（uptime 重置会低值持续数分钟；`mysql_up=0` 的瞬态在 15s 抓取下几乎抓不到），
必要时取**并集**。

## 形态二：恢复判据的语义写反

`recovered_signals` 的语义是"**要消失的症状**"（必须变为**假**才算恢复），
不是"恢复后应成立的条件"。写成：

```promql
count(up{job="payment-app"} == 1) >= 3      # 健康时为真 → 永远等不到它为假
```

实测证据极其直白：

```
recovery_checks: ok=False waited=150.0s
  expr   = count(up{job="payment-app"} == 1) >= 3
  result = ['3']            ← 条件明明已满足，检查却判失败
```

## 形态三：门的前置条件排在门之后（CI）

CI 用 `doctor`（**包含"数据集体量"检查**）当就绪门，却把 `seed` 放在门**之后**：

```
bootstrap → [门: doctor 重试] → seed → 最终 doctor
                ↑ 永远不可能通过（数据集还没建）
```

表现是"重试 15 次后失败"，看起来像"栈坏了"。

**修法**：门的全部前置条件必须**已经在它之前**完成。

## 提炼

1. 判据的**时间窗口**必须短于等待，且要考虑**证据出现的时间**。
2. 恢复判据写"要消失的症状"，不是"恢复后的期望状态"。
3. 用完整验收检查当门时，**门的前置条件必须在门之前**。
4. 判据必须验证**它名字所承诺的东西**：叫"真实 OOMKill"就必须读 `oom_kill` 计数器，
   不能只看内存压力（否则一个什么都没做的注入也会"通过"）。

这些已写入 [CONTRIBUTING.md](../../CONTRIBUTING.md) 的"不可协商的纪律"。
