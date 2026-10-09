# -*- coding: utf-8 -*-
"""
第 4 轮本体增量：给根因术语补 `remediation`（处置动作）—— 发布 ontology_v4。

## 这一轮要解决什么

第 11.20 节把「根因结论」从"资源类 / rc:tmp-disk"补全成了
「名称 + 机制 + 归因对象 + 观测佐证 + 判据 + 命中依据」——
但**结论的"怎么办"这一半仍然是按类别给的通用动作**：

    资源类 → [P0] 确认容器是否因内存/CPU 受限被杀或重启

对一个「磁盘临时表」的根因，这句话既不准确也不可执行（真正该做的是
看 `Created_tmp_disk_tables` 速率、调 `tmp_table_size`、改写 GROUP BY 让它命中索引）。

根因是：**处置知识压根不存在于本体里**。`_suggest_actions(category)` 是代码里
按"类别"硬编码的一张表 —— 类别只有 5 个（数据/资源/配置/依赖/代码），
而根因有 15 个，粒度天然对不上。

## 本轮做法

给 15 个 `rc:` 根因术语补上 `remediation`：一组**同构于 next_actions 的动作**
（`{urgency, action, command, needs_approval}`），于是：

  · "怎么办"与"是什么/为什么/在哪/凭什么"一样，都来自本体、可评审、可版本化；
  · 动作带 `needs_approval`，把"只读排查"与"需要审批的写操作"显式分开
    （本项目的硬边界：诊断只读，写操作一律需人工审批）；
  · 引擎取用顺序变为「术语的 remediation → 兜底按类别」，不再一上来就给通用话。

## 为什么字段放在 Term 上而不是新建 Constraint

EvoOntology 的 Constraint 表达的是"可判定的规则/阈值"（触发即判定），
而处置动作不是判定条件、也不参与打分 —— 它是在**结论已经确定之后**才被消费的。
放 Term 上语义正确，也不会污染打分索引（`scoring_keywords` 才参与打分）。

## schema 说明

`remediation` 是本项目对 EvoOntology Term schema 的**扩展字段**，
与第 2 轮的 `scoring_keywords` / `negative_keywords`、第 3 轮的 `lifecycle` 同类：
`validate()` 只强制 `id`，额外字段可安全共存，EvoOntology 运行时忽略未知字段。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

NOW = "2026-10-05T15:20:00Z"


def _a(urgency: str, action: str, command: str = "",
       needs_approval: bool = False) -> Dict[str, Any]:
    """一条处置动作（与 backend 的 next_actions 同构）。"""
    return {"urgency": urgency, "action": action, "command": command,
            "needs_approval": needs_approval}


#: 每个根因术语的处置动作。原则：
#:   1. **P0 一定是只读的**（先把事实查清），把写操作放到 P1 并要求审批；
#:   2. `command` 给可直接复制的只读命令；
#:   3. 动作要指向**这个根因特有的杠杆**，不能是"检查一下资源"这种放之四海的废话。
REMEDIATION: Dict[str, List[Dict[str, Any]]] = {
    "rc:slow-sql": [
        _a("P0", "对聚合语句做 EXPLAIN，确认是否 type=ALL 全表扫描 + Using filesort",
           "EXPLAIN <慢SQL>（或 db_explain 工具）"),
        _a("P1", "为聚合/过滤条件补索引（t_txn.created_at、t_txn.customer_id）",
           "ALTER TABLE t_txn ADD INDEX ...", True),
        _a("P1", "把应用层遍历大表的聚合下推到 SQL 侧并加 LIMIT，避免亿级行扫描",
           "", True),
    ],
    "rc:replica-loss": [
        _a("P0", "列出全部副本（含已退出）并查看缺失副本的退出码 / OOMKilled",
           "docker ps -a --filter name=payment-app"),
        _a("P0", "查看缺失副本的最后日志，区分崩溃、被杀与 OOMKill",
           "docker logs --tail 100 <replica>"),
        _a("P1", "恢复该副本；若因 OOMKill 反复重启，则上调内存限额或增加副本数",
           "docker start <replica>", True),
    ],
    "rc:replica-hang": [
        _a("P0", "对比「容器 Up」与「健康探针失败」，确认是进程在但不干活（假死而非崩溃）",
           "docker inspect -f '{{.State.Status}}' <replica> + /health 探活"),
        _a("P0", "确认该副本是否仍在承接流量（假死副本会造成部分请求永久挂起）",
           "查看应用层实例级 QPS/P99 分布"),
        _a("P1", "先摘流量再重启该副本（假死进程无法自行恢复）",
           "docker restart <replica>", True),
    ],
    "rc:oom-kill": [
        _a("P0", "确认 OOM 证据：OOMKilled 标志与 RestartCount",
           "docker inspect -f '{{.State.OOMKilled}} {{.RestartCount}}' <container>"),
        _a("P0", "对比 cgroup 内存上限与实际用量峰值，判断是限额过低还是内存泄漏",
           "cc_container_memory_working_set_bytes / _limit_bytes"),
        _a("P1", "上调 mem_limit，或定位并修复内存增长点（二者选一，避免掩盖泄漏）",
           "", True),
    ],
    "rc:cpu-throttle": [
        _a("P0", "读 cgroup 节流计数确认被限流，而不是单纯压力大",
           "rate(cc_container_cpu_cfs_throttled_periods_total[5m])"),
        _a("P0", "确认是否只有单个副本延迟偏高（配额问题 → 副本间不均衡）",
           "按 app_instance 对比 P99"),
        _a("P1", "提高 cpus 配额；或在配额不变的前提下增加副本数分摊负载",
           "", True),
    ],
    "rc:gateway-down": [
        _a("P0", "绕过网关直接探活各副本 :8080/health，区分「入口挂」与「后端挂」",
           "逐个副本 /health 探活"),
        _a("P0", "确认网关容器状态与上游配置（副本 IP 变化后 upstream 可能失效）",
           "docker inspect cc-app-gateway / nginx upstream 配置"),
        _a("P1", "恢复网关入口；并评估多实例或健康检查自动重启，消除单点",
           "docker start cc-app-gateway", True),
    ],
    "rc:net-fault": [
        _a("P0", "对比 blackbox 探活延迟/丢包与副本自身延迟，确认瓶颈在链路而非应用",
           "probe_duration_seconds vs 应用内 P99"),
        _a("P0", "确认 CPU/内存等资源指标正常（网络类故障的特征是资源全绿）",
           "cc_container_cpu_seconds / _memory_working_set_bytes"),
        _a("P1", "检查容器网络与 tc/netem 规则，清理注入残留",
           "docker exec <c> tc qdisc show", True),
    ],
    "rc:cluster-capacity": [
        _a("P0", "核对在册副本数与单副本配额是否与当前 QPS 匹配（多数副本同时受限=整体容量不足）",
           "副本数 / cpus / mem_limit vs rate(cc_http_requests_total[5m])"),
        _a("P1", "扩容：增加副本数，或上调单副本配额",
           "deploy.replicas / cpus", True),
        _a("P2", "评估入口限流与排队策略，避免容量不足时雪崩",
           "", True),
    ],
    "rc:row-lock": [
        _a("P0", "找出持锁者与等待者（长事务的 trx_query / trx_rows_locked）",
           "SELECT * FROM information_schema.innodb_trx; "
           "SELECT * FROM sys.innodb_lock_waits"),
        _a("P0", "注意别只看 Innodb_row_lock_current_waits：会话被 kill 时它漏减，"
                 "会永久卡在非 0（须用 rate(Innodb_row_lock_waits[2m]) 判断）",
           "rate(mysql_global_status_innodb_row_lock_waits[2m])"),
        _a("P1", "终止长事务会话释放锁",
           "KILL <trx_mysql_thread_id>", True),
        _a("P1", "把全表 FOR UPDATE 收窄为主键/索引范围锁，并缩短事务持有时间",
           "", True),
    ],
    "rc:conn-exhaust": [
        _a("P0", "对比 threads_connected / max_connections 与连接池上限，确认是谁在占连接",
           "SHOW STATUS LIKE 'Threads_connected'; SHOW VARIABLES LIKE 'max_connections'"),
        _a("P0", "按 user/host/时间列出长 Sleep 连接，定位连接泄漏来源",
           "SELECT user, host, time, command FROM information_schema.processlist "
           "WHERE command='Sleep' ORDER BY time DESC"),
        _a("P1", "清理空闲会话并修复泄漏；必要时收敛连接池上限",
           "KILL <id>", True),
    ],
    "rc:db-cpu-starve": [
        _a("P0", "确认数据库并发执行线程高企（排队而非单条慢）",
           "SHOW STATUS LIKE 'Threads_running'"),
        _a("P0", "找出消耗 CPU 最重的语句（交叉连接 / 无谓笛卡尔积是典型）",
           "slow query top / performance_schema.events_statements_summary_by_digest"),
        _a("P1", "终止重查询，并改写为可下推的关联查询",
           "KILL <id>", True),
    ],
    "rc:tmp-disk": [
        _a("P0", "确认磁盘临时表速率与内存临时表阈值的当前值（判断是排序内存不足而非磁盘故障）",
           "rate(mysql_global_status_created_tmp_disk_tables[2m]); "
           "SHOW VARIABLES LIKE 'tmp_table_size'; SHOW VARIABLES LIKE 'sort_buffer_size'"),
        _a("P0", "对触发落盘的语句做 EXPLAIN，确认 GROUP BY / ORDER BY 是否 Using filesort",
           "EXPLAIN <慢SQL>"),
        _a("P1", "上调 tmp_table_size / sort_buffer_size（可先在会话级验证再改全局）",
           "SET SESSION tmp_table_size=...", True),
        _a("P1", "改写 GROUP BY / ORDER BY 使其命中索引，从根上消除 filesort 落盘",
           "", True),
    ],
    "rc:primary-readonly": [
        _a("P0", "确认主库是否被置为只读（健康检查会全绿，但写入全部 1290 失败）",
           "SELECT @@read_only, @@super_read_only"),
        _a("P0", "确认写入失败的报错码与影响面（交易是否全部失败而读仍成功）",
           "应用日志 / 错误码 1290"),
        _a("P1", "复位 read_only=OFF 恢复写入",
           "SET GLOBAL read_only=OFF", True),
        _a("P1", "把 read_only / super_read_only 纳入配置巡检，防止再次漂移",
           "", True),
    ],
    "rc:replica-lag": [
        _a("P0", "确认复制线程状态与落后量（IO/SQL 都 Running 也可能在落后）",
           "SHOW REPLICA STATUS\\G（Seconds_Behind_Source / GTID gap）"),
        _a("P0", "确认读到的数据是否过期（副本回放能力不足导致读到旧值）",
           "对比主从同一主键的 updated_at"),
        _a("P1", "弱化对副本的一致性依赖：强一致读切回主库",
           "", True),
        _a("P1", "若为回放能力不足，降低主库批量写入或提升副本资源",
           "", True),
    ],
    "rc:db-replica-loss": [
        _a("P0", "确认复制线程是否停止与副本节点是否可达",
           "SHOW REPLICA STATUS\\G；exporter 抓取目标是否 down"),
        _a("P0", "确认读路径是否会真的打到该副本（读轮转是否把它计入）",
           "SELECT COUNT(*) FROM information_schema.processlist WHERE user='appuser'"),
        # 注意：这一条**不是只读**（把副本摘出读轮转是运维变更），
        # 因此按本项目不变式必须是 P1 + needs_approval，不能占 P0。
        _a("P1", "把读流量从该副本摘除，避免继续读到旧数据",
           "", True),
        _a("P1", "恢复复制（修 SOURCE_HOST / START REPLICA），并确认只读副本重新在册",
           "START REPLICA", True),
    ],
}


def check_invariants() -> List[str]:
    """
    处置动作的硬不变式（违反即视为补丁本身有缺陷，发布前必须拦住）。

    ① **P0 必须只读**（`needs_approval=False`）。
       理由：本项目的边界是"诊断只读、写操作必须人工审批"。如果 P0 里混进需要审批的
       写操作，界面上一眼看去"最紧急的事"就变成了"需要等人批准的事"，
       紧急度分级失去意义，也会诱导自动化去执行写操作。
       （第一次写这个补丁时就违反过一次：`rc:db-replica-loss` 把"摘读流量"标成了 P0。）
    ② 每条动作必须有非空 `action`，urgency ∈ {P0,P1,P2}，且显式声明 `needs_approval`。
    """
    problems: List[str] = []
    for tid, actions in REMEDIATION.items():
        for a in actions:
            if a.get("urgency") not in ("P0", "P1", "P2"):
                problems.append("%s: 非法 urgency=%r" % (tid, a.get("urgency")))
            if not str(a.get("action") or "").strip():
                problems.append("%s: 空 action" % tid)
            if not isinstance(a.get("needs_approval"), bool):
                problems.append("%s: needs_approval 未显式声明" % tid)
            if a.get("urgency") == "P0" and a.get("needs_approval"):
                problems.append("%s: P0 动作要求审批（违反「P0 只读」不变式）: %s"
                                % (tid, str(a.get("action"))[:40]))
    return problems


def build_patch(base_term_ids: set | None = None,
                base_evidence_ids: set | None = None,
                base_mapping_ids: set | None = None
                ) -> Dict[str, List[Dict[str, Any]]]:
    """
    只产 `terms` 族：给既有根因术语 upsert `remediation` 字段。

    补丁语义是字段级合并（见 evolve 的 _apply_patch），所以这里**只给 id + remediation**，
    父版本里该术语的其它字段（name/definition/scoring_keywords/lifecycle…）原样保留。
    """
    base_term_ids = base_term_ids or set()
    terms: List[Dict[str, Any]] = []
    for tid, actions in REMEDIATION.items():
        if base_term_ids and tid not in base_term_ids:
            # 父版本里没有这个术语 → 本轮不负责创建它（处置知识不能凭空造概念）
            continue
        terms.append({"id": tid, "remediation": actions})
    return {"terms": terms, "mappings": [], "relations": [],
            "constraints": [], "evidence": []}


#: 命令里可能引用的 Prometheus 指标名（用于"引用必须真实存在"的校验）
_METRIC_RE = re.compile(
    r"\b(cc_[a-z0-9_]+|mysql_global_status_[a-z0-9_]+|mysql_global_variables_[a-z0-9_]+"
    r"|probe_[a-z0-9_]+)\b")

#: CJK 字符 —— command 字段里出现即说明"把散文当命令"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def valid_metric_names(prom: str = "http://localhost:9090") -> Optional[Set[str]]:
    """
    从 Prometheus 拉取**当前真实存在**的全部指标名。拿不到返回 None（调用方跳过该检查）。

    为什么需要它：`remediation_contract` 原本只检查"动作是否具体/urgency 合法/是否声明审批"，
    这些都能在纸面上通过 —— 而实测发现 `rc:cpu-throttle` 的第一条 P0 命令写的是
    `rate(cc_container_cpu_cfs_throttled_periods_total[5m])`，
    **这个指标根本不存在**（正确的是 `cc_container_cpu_throttled_seconds_total`）。
    照着这条"建议"去查，只会得到一个空结果 —— 与"恒为绿"的证据同样危险。
    """
    try:
        import urllib.request
        url = prom + "/api/v1/label/__name__/values"
        d = json.loads(urllib.request.urlopen(url, timeout=20).read().decode())
        vals = d.get("data") or []
        return {str(v) for v in vals} if vals else None
    except Exception:  # noqa: BLE001
        return None


def term_command_problems(term: Dict[str, Any],
                          valid_metrics: Optional[Set[str]] = None) -> List[str]:
    """
    该术语 remediation 里 **command 字段本身的问题**（两条）：

      ① 引用了不存在的 Prometheus 指标（仅当传入 valid_metrics 时校验）
      ② command 字段里混进了说明性文字（含中日韩字符）

    第 ② 条的口径：`command` 只放**可直接执行的命令**，说明写在 `action` 里。
    踩过的坑：v4 里有 12 条 P0 动作把散文写进了 command
    （例如 `按 app_instance 对比 P99`、`逐个副本 /health 探活`），
    界面却按等宽命令渲染 —— 既误导操作人，也无法被自动验证。
    真实命令（PromQL/SQL/docker）在本项目里都是 ASCII，占位符也统一用
    `<slow-sql>` / `<replica>` 这种 ASCII 形式，因此"含 CJK 即非法"是可判定的。
    """
    out: List[str] = []
    for a in term.get("remediation") or []:
        if not isinstance(a, dict):
            continue
        cmd = str(a.get("command") or "")
        if valid_metrics:
            for m in sorted(set(_METRIC_RE.findall(cmd))):
                if m not in valid_metrics:
                    out.append("引用了不存在的指标 %s（命令：%s）" % (m, cmd[:48]))
        if _CJK_RE.search(cmd):
            out.append("command 含说明性文字（应移入 action）：%s" % cmd[:48])
    return out


def remediation_contract(term: Dict[str, Any],
                         valid_metrics: Optional[Set[str]] = None) -> Dict[str, Any]:
    """
    单个根因术语的处置契约（用于 ground truth 逐 case 打分 0~1）。

      ① 至少有 1 条 action，且 action 文本非空且足够具体（≥12 字，拒绝"检查一下"式空话）
      ② 每条 action 都有 urgency，且取值在 P0/P1/P2
      ③ 每条 action 都显式声明 needs_approval（布尔）——
         本项目硬边界：诊断只读，写操作必须标"需人工审批"，不允许默认省略
      ④ **命令里引用的指标必须真实存在**（仅当传入 valid_metrics 时校验）
      ⑤ **command 字段不得含说明性文字**（含 CJK 即视为把散文当命令）

    为什么不用"有没有 remediation 字段"这种 0/1 口径：那种口径下
    `remediation: [{}]` 也能拿满分，等于没验证。这里逐条检查**可用性**。

    ④⑤ 两条都是实战打出来的（ontology_v5 那一轮）：
    前三项都能在纸面上通过，但命令可能指向不存在的指标、或者**根本不是命令**。
    """
    items = [x for x in (term.get("remediation") or []) if isinstance(x, dict)]
    ok1 = any(str(x.get("action") or "").strip() and len(str(x.get("action")).strip()) >= 12
              for x in items)
    ok2 = bool(items) and all(str(x.get("urgency") or "") in ("P0", "P1", "P2") for x in items)
    ok3 = bool(items) and all(isinstance(x.get("needs_approval"), bool) for x in items)
    ok5 = not any(_CJK_RE.search(str(x.get("command") or "")) for x in items)
    checks = {"has_specific_action": ok1, "urgency_valid": ok2,
              "approval_declared": ok3, "command_is_command": ok5}
    if valid_metrics:
        checks["command_targets_exist"] = not [
            p for p in term_command_problems(term, valid_metrics)
            if "不存在的指标" in p]
    passed = list(checks.values())
    return {
        "score": round(sum(1 for p in passed if p) / len(passed), 4) if passed else 0.0,
        "items": len(items),
        "checks": checks,
    }

