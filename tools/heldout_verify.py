# -*- coding: utf-8 -*-
"""
Held-out 复验：现有本体/打分词典**是否只是背下了那 21 个场景**。

## 为什么要单独做这件事

21 个场景的告警文本、`scoring_keywords`、`negative_keywords` 与阈值**都是按它们人工调过的**
（§11.21 第 2 项）。于是"严格命中 81%~86%"这个数字里，
有多少是"真的会诊断"、有多少是"见过这道题"，**从训练集上是看不出来的**。
本脚本用两类**没参与调参**的输入来回答：

1. **held-out 告警措辞**（10 条）：同一个故障、换个人换种说法写告警 ——
   刻意避开调参时依赖的关键词（例如不说"节流"而说"CPU 被限速"、
   不说"磁盘临时表"而说"排序落盘"）。测的是**文本 → 本体**的映射是否稳健。
2. **held-out 故障机制**（2 个）：**同一根因、不同的注入手法** ——
   ① 用 `docker update --cpus` 压低配额（而不是跑 stress-ng 打满 CPU）；
   ② 把 `max_connections` 调小（而不是灌入 170 个连接）。
   测的是**"从现象回到根因"是否只对某一种造法有效**。

两者都不改本体、不改词典 —— 它们是"考卷"，不是"教材"。

用法：
  python tools/heldout_verify.py                # 只跑离线措辞部分（秒级）
  python tools/heldout_verify.py --with-faults  # 另跑 2 个真实注入（约 5 分钟）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca-agent"))
sys.path.insert(0, str(ROOT / "rca-agent" / "backend"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

# ═══════════════════════════════════════════════════════════════════════════
# 1) held-out 告警措辞（未参与调参）
# ═══════════════════════════════════════════════════════════════════════════
#: 写法原则：**换一个人、换一套词**，且刻意避开原告警注解里的关键词。
HELDOUT_PHRASINGS: List[Dict[str, Any]] = [
    {
        "id": "ho-01", "for_fault": "app_cpu_stress",
        "text": "订单服务的处理线程被操作系统限速了，单台机器的算力被压到配额上限，请求排队变长",
        "expected": ["rc:cpu-throttle"],
    },
    {
        "id": "ho-02", "for_fault": "app_kill_replica",
        "text": "支付集群里有一台机器不见了，负载均衡把流量分给了剩下的两台",
        "expected": ["rc:replica-loss"],
    },
    {
        "id": "ho-03", "for_fault": "app_gateway_stop",
        "text": "外部用户报告网站打不开，入口那一层返回 502，后端服务本身是活的",
        "expected": ["rc:gateway-down"],
    },
    {
        "id": "ho-04", "for_fault": "db_slow_query_flood",
        "text": "数据库里有一批语句扫描了整个表，没有走索引，执行时间从毫秒涨到了秒级",
        "expected": ["rc:slow-sql"],
    },
    {
        "id": "ho-05", "for_fault": "db_row_lock_hold",
        "text": "某个事务一直不提交，它锁住的那几行别人改不了，写入都在等",
        "expected": ["rc:row-lock"],
    },
    {
        "id": "ho-06", "for_fault": "db_primary_readonly",
        "text": "下单接口全部报错，但查询接口正常；数据库那边说这台机器不允许写",
        "expected": ["rc:primary-readonly"],
    },
    {
        "id": "ho-07", "for_fault": "db_replica_lag",
        "text": "从库追不上主库，报表读到的数据比实际晚了一分钟左右",
        "expected": ["rc:replica-lag"],
    },
    {
        "id": "ho-08", "for_fault": "db_conn_saturation",
        "text": "应用拿不到新的数据库会话，池子已经满了，等待队列在涨",
        "expected": ["rc:conn-exhaust"],
    },
    {
        "id": "ho-09", "for_fault": "app_oom_kill",
        "text": "这台机器的内存用量顶到了上限，内核把它里面的进程干掉了，容器重启了一次",
        "expected": ["rc:oom-kill"],
    },
    {
        "id": "ho-10", "for_fault": "res_db_memory",
        "text": "排序和分组需要的空间超过了内存配置，结果写到了磁盘上，查询因此变慢",
        "expected": ["rc:tmp-disk"],
    },
]


def eval_phrasings(top_k: int = 5) -> Dict[str, Any]:
    """离线：把 held-out 措辞喂给确定性引擎，看 top1/topk 是否命中期望术语。"""
    from backend.services.rca_engine import RCAEngine
    eng = RCAEngine()
    nodes = eng.traverse_topology()
    rows = []
    for c in HELDOUT_PHRASINGS:
        parsed = eng.parse_alert(c["text"], "P1")
        cands = eng.score_candidates(nodes, parsed)
        ids = [str(x["entity_id"]) for x in cands]
        top1 = ids[0] if ids else ""
        rows.append({
            "id": c["id"], "for_fault": c["for_fault"], "text": c["text"],
            "expected": c["expected"], "top1": top1,
            "top1_hit": top1 in c["expected"],
            "topk_hit": any(i in c["expected"] for i in ids[:top_k]),
            "top5": ids[:5],
            "role": (cands[0].get("role") if cands else ""),
        })
    n = len(rows)
    return {
        "n": n,
        "top1": sum(1 for r in rows if r["top1_hit"]),
        "topk": sum(1 for r in rows if r["topk_hit"]),
        "rows": rows,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 2) held-out 故障机制（同根因、不同注入手法）
# ═══════════════════════════════════════════════════════════════════════════
#: 每个 held-out 故障给出 (注入函数, 回滚函数)；注入手法与 catalog 里那 21 个**不同**。
HELDOUT_FAULTS: List[Dict[str, Any]] = [
    {
        "id": "ho-fault-cpu-quota",
        "title": "压低单副本 CPU 配额（docker update，而非跑压测打满）",
        "expected": ["rc:cpu-throttle"],
        "container": "rca-agent-payment-app-2",
    },
    {
        "id": "ho-fault-maxconn",
        "title": "调小 max_connections（而非灌入 170 个连接）",
        "expected": ["rc:conn-exhaust"],
        "container": "cc-mysql-core",
    },
]


def _docker(args: List[str], timeout: int = 60):
    import subprocess
    return subprocess.run(["docker"] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _mysql(sql: str, user: str = "root", pwd: str = "rootpass") -> None:
    _docker(["exec", "cc-mysql-core", "mysql", "-u%s" % user, "-p%s" % pwd,
             "creditcard", "-e", sql])


def inject_holdout(fid: str) -> Dict[str, Any]:
    if fid == "ho-fault-cpu-quota":
        # 先把配额压到 0.05 核：不需要压测，容器自身就会频繁被节流
        r = _docker(["update", "--cpus", "0.05", "rca-agent-payment-app-2"])
        return {"ok": r.returncode == 0, "detail": (r.stderr or r.stdout)[:200]}
    if fid == "ho-fault-maxconn":
        _mysql("SET GLOBAL max_connections=60")
        # 应用侧本来就握着 ~37 个连接，再灌一批把池子打满。
        # ⚠ 这些会话是 `SELECT SLEEP(300)`：命令**不会自己返回**，
        # 所以既不能用默认 timeout（会抛 TimeoutExpired 把整个测试打断），
        # 也不能等它 —— 用最长 5s 的短超时，超时即视为"会话已在后台挂住"。
        try:
            _docker(["exec", "-d", "cc-mysql-core", "sh", "-c",
                     "for i in $(seq 1 30); do mysql -uappuser -papppass -h127.0.0.1 "
                     "creditcard -e \"SELECT SLEEP(300) /* ho-maxconn */\" "
                     ">/dev/null 2>&1 & done"], timeout=20)
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "detail": {"max_connections": 60,
                                           "note": "会话已在后台挂住（%s）" % str(e)[:60]}}
        time.sleep(5)
        return {"ok": True, "detail": {"max_connections": 60, "extra_sessions": 30}}
    return {"ok": False, "detail": "未知 held-out 故障"}


def recover_holdout(fid: str) -> Dict[str, Any]:
    if fid == "ho-fault-cpu-quota":
        r = _docker(["update", "--cpus", "0.5", "rca-agent-payment-app-2"])
        return {"ok": r.returncode == 0}
    if fid == "ho-fault-maxconn":
        from tools.chaos.core import kill_by_marker
        killed = kill_by_marker("cc-mysql-core", "ho-maxconn")
        _mysql("SET GLOBAL max_connections=200")
        return {"ok": True, "detail": killed}
    return {"ok": False}


def eval_heldout_fault(fid: str, timeout_s: float = 240.0) -> Dict[str, Any]:
    """
    注入 held-out 故障 → 等告警 → 直接调后端诊断（走真实链路）→ 比对期望术语。
    注入手法不在 catalog 里，因此这里自己做注入/回滚。
    """
    import urllib.request
    from tools.chaos.core import prom_query

    meta = next(c for c in HELDOUT_FAULTS if c["id"] == fid)
    out: Dict[str, Any] = {"id": fid, "title": meta["title"], "expected": meta["expected"]}
    try:
        # ★ 先记下**注入前**就已在 firing 的告警：它们是上一步的残留。
        # 第一版直接取"当前所有 firing 告警"当本次输入，于是把残留的
        # MySQLDiskTempTables/MySQLSlowQueries 喂进引擎 → 引擎答 rc:tmp-disk
        # （对那段文本其实是对的），却被判成 MISS —— 这是**取数错**，不是引擎错。
        # 端到端脚本早就有这道区分，这里当时漏了。
        pre = {str(x["metric"].get("alertname"))
               for x in (prom_query('ALERTS{alertstate="firing"}') or [])}
        out["pre_firing"] = sorted(pre)
        out["inject"] = inject_holdout(fid)
        if not out["inject"].get("ok"):
            out["error"] = "注入失败"
            return out
        # 等一条**本次新响**的真实告警（而不是"有告警就行"）
        deadline = time.time() + timeout_s
        fired: List[str] = []
        while time.time() < deadline:
            r = prom_query('ALERTS{alertstate="firing"}')
            now = {str(x["metric"].get("alertname")) for x in (r or [])}
            fired = sorted(now - pre)
            if fired:
                break
            time.sleep(10)
        out["alerts_firing"] = fired
        out["residual_alerts"] = sorted({str(x["metric"].get("alertname"))
                                         for x in (prom_query('ALERTS{alertstate="firing"}') or [])}
                                        - set(fired))
        # 取这些告警的 rca_hint 作为诊断输入（与端到端脚本同口径）
        body = json.loads(urllib.request.urlopen(
            "http://localhost:9090/api/v1/rules", timeout=20).read().decode())
        hints = []
        for g in (body.get("data") or {}).get("groups") or []:
            for rule in g.get("rules") or []:
                if rule.get("name") in fired and (rule.get("annotations") or {}).get("rca_hint"):
                    hints.append(rule["annotations"]["rca_hint"])
        out["alert_text"] = "；".join(hints[:4]) or "监控无对应告警，但业务侧出现异常（层级=数据库层）"
        req = urllib.request.Request(
            "http://localhost:8088/api/agent/diagnose",
            data=json.dumps({"alert": out["alert_text"], "severity": "P1",
                             "mode": "deterministic", "app_name": "payment-app",
                             "persist": False}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=180).read().decode())
        rc = (d.get("root_cause") or {}).get("entity_id") or ""
        out["top1"] = rc
        out["top1_hit"] = rc in meta["expected"]
        out["mode"] = d.get("mode") or d.get("status")
    finally:
        out["recover"] = recover_holdout(fid)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Held-out 复验（措辞 + 故障机制）")
    ap.add_argument("--with-faults", action="store_true", help="另跑 2 个真实注入（约 5 分钟）")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "heldout_report.json"))
    args = ap.parse_args(argv)

    print("=" * 92)
    print("Held-out 复验：这些输入**没有参与过调参**")
    print("=" * 92)

    ph = eval_phrasings()
    print("\n【1】held-out 告警措辞（%d 条，离线喂给确定性引擎）" % ph["n"])
    print("  %-7s %-22s %-26s %-6s %s" % ("id", "对应故障", "top1", "命中", "期望"))
    for r in ph["rows"]:
        print("  %-7s %-22s %-26s %-6s %s"
              % (r["id"], r["for_fault"], r["top1"][:26], "OK" if r["top1_hit"] else "MISS",
                 ",".join(r["expected"])))
    print("  → top1 **%d/%d**，top5 **%d/%d**"
          % (ph["top1"], ph["n"], ph["topk"], ph["n"]))

    faults: List[Dict[str, Any]] = []
    if args.with_faults:
        print("\n【2】held-out 故障机制（同根因、不同注入手法）")
        for f in HELDOUT_FAULTS:
            print("\n  ── %s：%s" % (f["id"], f["title"]))
            r = eval_heldout_fault(f["id"])
            faults.append(r)
            print("     注入=%s  告警=%s" % (r.get("inject", {}).get("ok"), r.get("alerts_firing")))
            print("     输入=%s" % str(r.get("alert_text"))[:100])
            print("     top1=%s  %s  期望=%s"
                  % (r.get("top1"), "OK" if r.get("top1_hit") else "MISS", r.get("expected")))
            print("     回滚=%s" % (r.get("recover") or {}).get("ok"))
        ok = sum(1 for r in faults if r.get("top1_hit"))
        print("\n  → held-out 故障 top1 **%d/%d**" % (ok, len(faults)))

    out = {"phrasings": ph, "faults": faults}
    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print("\n报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
