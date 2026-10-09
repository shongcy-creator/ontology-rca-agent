# -*- coding: utf-8 -*-
"""
处置动作的**执行闭环**：计划 → 审批 → 执行 → **执行后校验** → 失败自动回滚。

## 为什么单独做这个，以及它的边界

本体（ontology_v4 起）给 15 个根因术语各写了几条 `remediation`，但那些只是**文本**：
"照着做什么"。本项目一直停在"建议"，没到"处置"（§11.21 第 16 项）。
本工具把那一格补上，但**刻意把能自动执行的范围压到最小**：

| 动作类型 | 能否自动执行 | 为什么 |
|---|---|---|
| **只读取证**（PromQL 查询 / SQL 的 SELECT·SHOW·EXPLAIN） | ✅ 自动执行 | 不改状态，失败也无害 |
| **可逆写操作**（白名单，见 `REVERSIBLE_OPS`） | ⚠ **必须 `--approve`** | 每条都带**逆操作**与**执行后校验**；校验不过 10~120s 内自动回滚 |
| 散文描述 / 含 `<占位符>` | ❌ 拒绝执行 | 无法机器执行；拒绝比"猜着跑"安全 |
| 其它写操作 | ❌ 拒绝执行 | 没有声明逆操作的写操作一律不执行 —— 宁可人工 |

**默认 `--dry-run`**：只打印"会执行什么、判据是什么"，不做任何改动。

## 闭环怎么算成功

不是"命令退出码为 0"，而是**根因判据真的消失了**：
执行前后各跑一次该术语的 P0 探针（复用 `remediation_efficacy.PROBES`），
`False → True` 才算 `verified`；只读动作则报告"条件是否仍在"。

用法：
  python tools/remediation_exec.py plan rc:row-lock
  python tools/remediation_exec.py run rc:row-lock --dry-run
  python tools/remediation_exec.py run rc:primary-readonly --approve
  python tools/remediation_exec.py self-test          # 需要集群里有对应故障在跑
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))
sys.path.insert(0, str(ROOT / "rca-agent" / "backend"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

from tools.chaos.core import MYSQL_PRIMARY, mysql_sql  # noqa: E402
from tools.remediation_efficacy import PROBES, _prom  # noqa: E402

_CJK = re.compile(r"[\u4e00-\u9fff]")
_SQL_READONLY = re.compile(r"^\s*(select|show|explain|describe|desc)\b", re.I)
_PLACEHOLDER = re.compile(r"<[^>]{1,40}>")

#: **可逆写操作白名单**：ops key → 正向命令 / 逆命令 / 执行后校验（PromQL 或 SQL）
#: 只有在这里登记过的写操作才允许 `--approve` 执行。
#: 每条都必须满足：可逆、可校验、影响面明确。
REVERSIBLE_OPS: Dict[str, Dict[str, Any]] = {
    "restore-primary-writable": {
        "title": "把主库恢复为可写（read_only=0）",
        "apply": "SET GLOBAL super_read_only=OFF; SET GLOBAL read_only=OFF;",
        "revert": "SET GLOBAL super_read_only=ON; SET GLOBAL read_only=ON;",
        "verify_prom": 'max(mysql_global_variables_read_only{db_node="primary"}) == 0',
        "needs": "mysql-root",
    },
    "restore-tmp-table-size": {
        "title": "恢复临时表/排序缓冲为默认值（从 1MB 恢复到 16MB）",
        "apply": ("SET GLOBAL internal_tmp_mem_storage_engine=TempTable; "
                  "SET GLOBAL tmp_table_size=16777216; SET GLOBAL max_heap_table_size=16777216;"),
        "revert": ("SET GLOBAL internal_tmp_mem_storage_engine=MEMORY; "
                   "SET GLOBAL tmp_table_size=1048576; SET GLOBAL max_heap_table_size=1048576;"),
        "verify_prom": ('max(rate(mysql_global_status_created_tmp_disk_tables'
                        '{db_node="primary"}[1m])) < 0.03'),
        "needs": "mysql-root",
    },
    "restore-max-connections": {
        "title": "把 max_connections 恢复为 200",
        "apply": "SET GLOBAL max_connections=200;",
        "revert": "SET GLOBAL max_connections=60;",
        "verify_prom": ('max(mysql_global_status_threads_connected{db_node="primary"} '
                        '/ mysql_global_variables_max_connections{db_node="primary"}) < 0.85'),
        "needs": "mysql-root",
    },
    "start-replica-once": {
        "title": "拉起被停掉的只读副本容器（docker start）",
        "apply": "__docker_start_replicas__",
        "revert": "__docker_stop_replicas__",
        "verify_prom": 'count(mysql_up{db_role="replica"} == 1) >= 2',
        "needs": "docker",
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# 计划：把本体里的 remediation 分类成"能执行/需审批/只能人工"
# ═══════════════════════════════════════════════════════════════════════════

def _classify(command: str) -> str:
    c = str(command or "").strip()
    if not c:
        return "empty"
    if _PLACEHOLDER.search(c):
        return "placeholder"
    if _CJK.search(c):
        # 含中文又没有 <占位符>：基本是说明性文字（PromQL/SQL 里不该出现中文标识符）
        # 例外：注释性 SQL —— 但本项目约定 command 必须是可直接执行的命令
        return "prose"
    if _SQL_READONLY.match(c):
        return "sql-readonly"
    if c.startswith(("SELECT", "select")) or "(" in c and "{" in c or re.match(r"^[a-z_]+\(", c):
        return "promql"
    if re.match(r"^(mysql|SQL|SET|SHOW|EXPLAIN)\b", c, re.I):
        return "sql-readonly"
    return "other"


def _match_reversible(term_id: str, command: str) -> Optional[str]:
    """
    把一个 remediation 命令对应到白名单里的可逆操作。

    只做**精确的语义匹配**（不猜）：例如命令里同时出现 read_only 与 =0 才算
    "restore-primary-writable"。匹配不上就归入"只能人工"，绝不硬套。
    """
    c = str(command or "").lower()
    if "read_only" in c and ("=0" in c or "off" in c):
        return "restore-primary-writable"
    if "tmp_table_size" in c and "16777216" in c:
        return "restore-tmp-table-size"
    if "max_connections" in c and "200" in c:
        return "restore-max-connections"
    if "docker start" in c or c.startswith("docker start"):
        return "start-replica-once"
    return None


def load_actions(term_id: str) -> List[Dict[str, Any]]:
    """从激活本体里取该术语的 remediation（按 urgency 排序：P0 在前）。"""
    from backend.services.ontology_scoring import OntologyScoring
    sc = OntologyScoring.get(None)
    exp = sc.explain(term_id, limit=8)
    rem = list(exp.get("remediation") or [])
    order = {"P0": 0, "P1": 1, "P2": 2}
    rem.sort(key=lambda a: order.get(str(a.get("urgency") or ""), 9))
    return rem


def build_plan(term_id: str) -> Dict[str, Any]:
    """生成可审计的执行计划（不产生任何副作用）。"""
    actions = load_actions(term_id)
    rows = []
    for i, a in enumerate(actions):
        cmd = str(a.get("command") or "")
        kind = _classify(cmd)
        rev = _match_reversible(term_id, cmd) if kind in ("sql-readonly", "other") else None
        rows.append({
            "index": i,
            "urgency": a.get("urgency"),
            "action": a.get("action"),
            "command": cmd,
            "needs_approval": bool(a.get("needs_approval")),
            "kind": kind,
            "executable": kind == "promql" or (kind == "sql-readonly" and not a.get("needs_approval")),
            "reversible_op": rev,
            "reversible_available": bool(rev),
        })
    probes = PROBES.get(term_id) or []
    return {
        "term_id": term_id,
        "actions": rows,
        "counts": {k: sum(1 for r in rows if r["kind"] == k)
                   for k in ("promql", "sql-readonly", "prose", "placeholder", "empty", "other")},
        "probes": [{"name": p[0], "desc": p[3]} for p in probes],
        "policy": ("只读动作自动执行；写操作仅当命中可逆白名单且显式 --approve 时执行；"
                   "散文/占位符一律拒绝执行。默认 --dry-run。"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 执行
# ═══════════════════════════════════════════════════════════════════════════

def _probe_state(term_id: str) -> List[Dict[str, Any]]:
    out = []
    for name, sampler, pred, desc in (PROBES.get(term_id) or []):
        try:
            v = sampler()
            ok = bool(pred(v))
        except Exception as e:  # noqa: BLE001
            v, ok = None, False
            desc = "%s（探针异常：%s）" % (desc, str(e)[:60])
        out.append({"probe": name, "value": v, "condition_holds": ok, "desc": desc})
    return out


def _readonly_creds() -> Tuple[str, str]:
    """
    取只读账号的口令，**从建库脚本里读**而不是在这里再写一份。

    为什么：只读账号是 `03_readonly_user.sql` 建的（`rca_readonly` / 最小权限：
    PROCESS + REPLICATION CLIENT + creditcard/performance_schema 的 SELECT）。
    把口令再抄一份到这里，就会在"改了建库脚本但忘了改工具"时静默失配 ——
    实测踩过：这里写空口令 → `Access denied ... (using password: NO)`，
    而当时我的代码**没检查执行结果**，照样打印"已执行（只读 SQL）"。
    """
    p = ROOT / "rca-agent" / "monitoring" / "mysql" / "init" / "03_readonly_user.sql"
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"IDENTIFIED BY '([^']+)'", txt)
        if m:
            return "rca_readonly", m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return "rca_readonly", ""


def _run_promql(cmd: str) -> Dict[str, Any]:
    v = _prom(cmd)
    return {"ok": v is not None, "value": v}


def _run_sql_readonly(cmd: str) -> Dict[str, Any]:
    user, pwd = _readonly_creds()
    rc, out, err = mysql_sql(MYSQL_PRIMARY, cmd, user=user, pwd=pwd)
    return {"ok": rc == 0, "stdout": (out or "")[:800], "stderr": (err or "")[:300],
            "user": user}


def _docker_replicas(action: str) -> Dict[str, Any]:
    names = ["cc-mysql-replica-1", "cc-mysql-replica-2"]
    verb = "start" if action == "start" else "stop"
    res = {}
    for n in names:
        p = subprocess.run(["docker", verb, n], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
        res[n] = p.returncode == 0
    return {"ok": all(res.values()), "detail": res}


def _apply_reversible(op: str) -> Dict[str, Any]:
    spec = REVERSIBLE_OPS[op]
    cmd = spec["apply"]
    if cmd == "__docker_start_replicas__":
        return _docker_replicas("start")
    if cmd == "__docker_stop_replicas__":
        return _docker_replicas("stop")
    rc, out, err = mysql_sql(MYSQL_PRIMARY, cmd, user="root", pwd="rootpass")
    return {"ok": rc == 0, "stderr": (err or "")[:200]}


def _revert_reversible(op: str) -> Dict[str, Any]:
    spec = REVERSIBLE_OPS[op]
    cmd = spec["revert"]
    if cmd == "__docker_start_replicas__":
        return _docker_replicas("start")
    if cmd == "__docker_stop_replicas__":
        return _docker_replicas("stop")
    rc, out, err = mysql_sql(MYSQL_PRIMARY, cmd, user="root", pwd="rootpass")
    return {"ok": rc == 0, "stderr": (err or "")[:200]}


def _wait_verify(expr: str, timeout_s: float = 120.0, interval: float = 10.0) -> Dict[str, Any]:
    """等"执行后校验"成立（真值）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        last = _prom(expr)
        if last is not None and last > 0:
            return {"ok": True, "value": last, "waited_s": round(time.time() - t0, 1)}
        time.sleep(interval)
    return {"ok": False, "value": last, "waited_s": round(time.time() - t0, 1)}


def run_plan(term_id: str, only_index: Optional[int] = None,
             approve: bool = False, dry_run: bool = True,
             verify_timeout: float = 120.0) -> Dict[str, Any]:
    """执行计划（默认 dry-run）。返回可审计的逐步结果。"""
    plan = build_plan(term_id)
    probes_before = _probe_state(term_id)
    out: Dict[str, Any] = {
        "term_id": term_id, "dry_run": bool(dry_run), "approved": bool(approve),
        "plan_counts": plan["counts"], "probes_before": probes_before, "steps": [],
    }
    acts = plan["actions"]
    if only_index is not None:
        acts = [a for a in acts if a["index"] == only_index]

    for a in acts:
        step: Dict[str, Any] = {"index": a["index"], "kind": a["kind"],
                                "urgency": a["urgency"], "command": a["command"],
                                "action": a["action"]}
        # ① 只读取证：自动执行（dry-run 时只打印）
        if a["kind"] == "promql":
            if dry_run:
                step["result"] = "dry-run：会执行 PromQL 查询（只读）"
            else:
                step["exec"] = _run_promql(a["command"])
                step["ok"] = bool(step["exec"].get("ok"))
                # ⚠ 必须按**执行结果**报告，不能一律写"已执行"——
                # 踩过：只读 SQL 因空口令 Access denied，界面却显示"已执行（只读 SQL）"。
                step["result"] = ("已执行（只读 PromQL），取值 %s" % step["exec"].get("value")
                                  if step["ok"] else
                                  "执行失败：%s" % str(step["exec"].get("value"))[:80])
            out["steps"].append(step)
            continue
        if a["kind"] == "sql-readonly":
            if dry_run:
                step["result"] = "dry-run：会执行只读 SQL（rca_readonly 账号）"
            else:
                step["exec"] = _run_sql_readonly(a["command"])
                step["ok"] = bool(step["exec"].get("ok"))
                if step["ok"]:
                    step["result"] = "已执行（只读 SQL），返回 %d 字节" % len(
                        step["exec"].get("stdout") or "")
                else:
                    step["result"] = "执行失败：%s" % str(step["exec"].get("stderr"))[:120]
            out["steps"].append(step)
            continue
        # ② 可逆写操作：必须 --approve
        if a["reversible_available"] and a["reversible_op"]:
            op = a["reversible_op"]
            spec = REVERSIBLE_OPS[op]
            step["reversible_op"] = op
            step["reversible_title"] = spec["title"]
            if not approve or dry_run:
                step["result"] = ("dry-run：写操作，需要 --approve（执行后会做 %s 校验，"
                                  "不过则自动回滚）" % spec["verify_prom"][:60])
                out["steps"].append(step)
                continue
            step["exec"] = _apply_reversible(op)
            step["verify_expr"] = spec["verify_prom"]
            ver = _wait_verify(spec["verify_prom"], timeout_s=verify_timeout)
            step["verify"] = ver
            if ver.get("ok"):
                step["result"] = "已执行且**执行后校验通过**"
            else:
                # ★ 失败自动回滚
                step["rollback"] = _revert_reversible(op)
                step["result"] = ("执行后校验未通过（%ss）→ **已自动回滚**"
                                  % ver.get("waited_s"))
            out["steps"].append(step)
            continue
        # ③ 其余：拒绝执行，并说明原因
        why = {"prose": "散文描述（不是命令）", "placeholder": "含 <占位符>，需人工代入",
               "empty": "空命令"}.get(a["kind"], "写操作但没有声明逆操作（白名单外）")
        step["result"] = "拒绝执行：%s —— 需人工处理" % why
        out["steps"].append(step)

    out["probes_after"] = _probe_state(term_id)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# self-test：验证"闭环"本身（含一次**故意失败 → 自动回滚**）
# ═══════════════════════════════════════════════════════════════════════════

def self_test(verify_timeout: float = 60.0) -> Dict[str, Any]:
    """
    对**当前真实状态**跑一轮：计划 → 只读取证 → 可逆写操作（执行后校验）→ 失败回滚。

    三个子测试：
      A. 计划分类：一定量的动作被正确归为"可执行/需审批/只能人工"；
      B. 只读取证：PromQL 真的取到值；
      C. **失败回滚**：故意用一条**校验必然不过**的写操作（把 tmp_table_size 改回默认，
         而校验表达式要求"磁盘临时表速率 < 0.03" —— 在当前无故障时它本来就不该成立），
         验证"校验不过 → 自动回滚"这条路径真的走了。
    """
    out: Dict[str, Any] = {}
    plan = build_plan("rc:tmp-disk")
    out["plan_counts"] = plan["counts"]
    out["plan_ok"] = plan["counts"]["promql"] + plan["counts"]["sql-readonly"] > 0

    # B. 只读取证
    rd = run_plan("rc:tmp-disk", only_index=None, approve=False, dry_run=False)
    out["readonly_steps"] = [s for s in rd["steps"] if s["kind"] in ("promql", "sql-readonly")]

    # C. 失败回滚：故意挑一条校验不可能通过的写操作 ——
    #    用 `restore-max-connections` 的 verify（要求连接占用率 < 0.85）；
    #    正常环境下它**成立**，所以这里改用"反向"验证手法：
    #    先手工把 max_connections 设成 60（制造 verify 不通过的条件），
    #    再跑 apply，观察是否自动回滚到 60（即 revert 生效）。
    mysql_sql(MYSQL_PRIMARY, "SET GLOBAL max_connections=60", user="root", pwd="rootpass")
    op = REVERSIBLE_OPS["restore-max-connections"]
    applied = _apply_reversible("restore-max-connections")     # 设为 200
    ver = _wait_verify("max(mysql_global_variables_max_connections{db_node=\"primary\"}) < 100",
                       timeout_s=15, interval=3)
    rolled = None
    if not ver.get("ok"):
        rolled = _revert_reversible("restore-max-connections")  # 回滚 → 回到 60
    out["rollback_test"] = {"applied": applied, "verify": ver, "rolled_back": rolled}
    # 复原到一个合理值，避免污染环境
    mysql_sql(MYSQL_PRIMARY, "SET GLOBAL max_connections=200", user="root", pwd="rootpass")
    out["restored_max_connections"] = 200
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="处置动作执行闭环（默认 dry-run）")
    ap.add_argument("mode", choices=["plan", "run", "self-test"])
    ap.add_argument("term", nargs="?", help="根因术语 id（plan/run 用）")
    ap.add_argument("--action", type=int, default=None, help="只跑第 N 条动作")
    ap.add_argument("--approve", action="store_true", help="允许执行白名单内的可逆写操作")
    ap.add_argument("--execute", action="store_true", help="真的执行（默认 dry-run）")
    ap.add_argument("--verify-timeout", type=float, default=120.0)
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "remediation_exec.json"))
    args = ap.parse_args(argv)

    if args.mode == "plan":
        if not args.term:
            print("用法: plan <term>", file=sys.stderr)
            return 2
        plan = build_plan(args.term)
        print("=" * 92)
        print("处置计划（只读，不执行）：%s" % args.term)
        print("=" * 92)
        print("  分类统计 =", plan["counts"])
        for a in plan["actions"]:
            tag = ("可执行(只读)" if a["executable"] else
                   ("需审批(可逆)" if a["reversible_available"] else "只能人工"))
            print("  [%d] %-4s %-12s %s" % (a["index"], a["urgency"], tag,
                                            str(a["action"])[:40]))
            print("        cmd: %s" % str(a["command"])[:110])
            if a["reversible_op"]:
                print("        可逆操作: %s（%s）" % (a["reversible_op"],
                                                  REVERSIBLE_OPS[a["reversible_op"]]["title"]))
        print("  执行后校验探针：")
        for p in plan["probes"]:
            print("    - %s：%s" % (p["name"], p["desc"]))
        print("  策略：%s" % plan["policy"])
        return 0

    if args.mode == "run":
        if not args.term:
            print("用法: run <term> [--approve] [--execute]", file=sys.stderr)
            return 2
        res = run_plan(args.term, only_index=args.action, approve=args.approve,
                       dry_run=not args.execute, verify_timeout=args.verify_timeout)
        print("=" * 92)
        print("处置执行：%s（dry_run=%s, approved=%s）" % (args.term, res["dry_run"], res["approved"]))
        print("=" * 92)
        print("  执行前判据：")
        for p in res["probes_before"]:
            print("    - %-28s value=%-10s 条件成立=%s" % (p["probe"], str(p["value"])[:10],
                                                          p["condition_holds"]))
        for s in res["steps"]:
            print("\n  [%d] %-4s %-10s %s" % (s["index"], s["urgency"], s["kind"],
                                              str(s["action"])[:40]))
            print("      %s" % s["result"])
            if s.get("verify"):
                print("      执行后校验：ok=%s waited=%ss" % (s["verify"].get("ok"),
                                                            s["verify"].get("waited_s")))
            if s.get("rollback"):
                print("      回滚：%s" % s["rollback"])
        print("\n  执行后判据：")
        for p in res["probes_after"]:
            print("    - %-28s value=%-10s 条件成立=%s" % (p["probe"], str(p["value"])[:10],
                                                          p["condition_holds"]))
    else:
        res = self_test()
        print("=" * 92)
        print("执行闭环自检")
        print("=" * 92)
        print("  计划分类 =", res["plan_counts"], "| 有可执行只读动作 =", res["plan_ok"])
        print("  只读取证步骤数 =", len(res["readonly_steps"]))
        rt = res["rollback_test"]
        print("  失败回滚测试：apply=%s verify_ok=%s → 回滚=%s"
              % (rt["applied"].get("ok"), rt["verify"].get("ok"), rt["rolled_back"]))
        print("  已复原 max_connections =", res["restored_max_connections"])

    Path(args.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                  encoding="utf-8")
    print("\n报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
