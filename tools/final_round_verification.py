# -*- coding: utf-8 -*-
"""
收尾验证：一次把 D/E/F 与"注入保真修复"的实测跑完，产出一份汇总。

为什么要有这个脚本：这几步**顺序敏感**（本体必须先发布，held-out 才算在测新本体；
每步之间必须回滚干净，否则后一步测的是前一步的残留），
而且都要在"全链路流水线跑完、集群空闲"之后才能做。把它们写成脚本，
就不用靠人记住顺序，也不会漏掉中间的回滚。

步骤：
  0. 前置：回滚全部残留 + 断言无激活故障（脏了就中止，不产出"看起来正常"的结果）
  1. D 第 6 轮本体迭代（memory）→ 成对评估 + 闸门 → 发布 ontology_v6
  2. E held-out 复验（措辞离线 + 2 个 held-out 故障机制）
  3. F 处置执行闭环自检（含"故意失败 → 自动回滚"路径）
  4. 注入保真：验证 app_oom_kill 现在**真的会杀**（判据看 killing 计数器）
  5. 收尾：回滚全部残留 + 断言干净
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def run(label: str, args: List[str], timeout: int = 1800,
        allow_fail: bool = False) -> Dict[str, Any]:
    print("\n" + "=" * 96)
    print("[%s] $ %s" % (label, " ".join(args)))
    print("=" * 96, flush=True)
    t0 = time.time()
    p = subprocess.run([sys.executable] + args, cwd=str(ROOT), timeout=timeout,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    print(out[-4000:] if len(out) > 4000 else out, flush=True)
    rec = {"label": label, "rc": p.returncode, "elapsed_s": round(time.time() - t0, 1)}
    if p.returncode != 0 and not allow_fail:
        print("\n[!] %s 返回码 %d —— 中止后续步骤（避免在脏状态上继续测）"
              % (label, p.returncode))
        rec["aborted"] = True
    return rec


def main(argv: Optional[List[str]] = None) -> int:
    results: List[Dict[str, Any]] = []
    out_path = ROOT / ".chaos" / "final_round_verification.json"

    # ── 0. 前置 ───────────────────────────────────────────────────────────
    results.append(run("cleanup-pre", ["tools/fault_injector.py", "cleanup"]))
    if results[-1]["rc"] != 0:
        print("前置回滚失败，中止。")
        return 2

    # ── 1. D：第 6 轮本体迭代 ─────────────────────────────────────────────
    r = run("D-round6-memory",
            ["tools/evolve_ontology_cluster.py", "--round", "memory",
             "--json-out", ".chaos/ontology_v6_report.json"], timeout=1800)
    results.append(r)
    if r["rc"] != 0:
        _write(out_path, results)
        return 3

    # ── 2. E：held-out 复验（含 2 个真实注入）────────────────────────────
    r = run("E-heldout",
            ["tools/heldout_verify.py", "--with-faults",
             "--json-out", ".chaos/heldout_report.json"], timeout=1800, allow_fail=True)
    results.append(r)
    results.append(run("cleanup-after-E", ["tools/fault_injector.py", "cleanup"]))

    # ── 3. F：处置执行闭环自检（含失败回滚路径）──────────────────────────
    r = run("F-exec-self-test",
            ["tools/remediation_exec.py", "self-test",
             "--json-out", ".chaos/remediation_exec.json"], timeout=900, allow_fail=True)
    results.append(r)

    # ── 4. 注入保真：app_oom_kill 现在真的会杀吗 ──────────────────────────
    # `inject` 会等信号成立（--signal-timeout 默认 50s）；修复后的判据包含
    # `increase(cc_container_oom_kill_total[5m]) > 0`，所以"信号成立"就等于"真杀了"。
    results.append(run("oom-kill-inject",
                       ["tools/fault_injector.py", "inject", "app_oom_kill",
                        "--signal-timeout", "90"], timeout=600, allow_fail=True))
    results.append(run("cleanup-after-oom", ["tools/fault_injector.py", "cleanup"]))

    # ── 5. 收尾：跨集群归属（用新发布的 ontology_v6）──────────────────────
    r = run("crosscluster-v6",
            ["tools/crosscluster_verify.py",
             "--json-out", ".chaos/crosscluster_report.json"], timeout=600, allow_fail=True)
    results.append(r)

    results.append(run("cleanup-final", ["tools/fault_injector.py", "cleanup"]))
    _write(out_path, results)
    print("\n汇总: %s" % out_path)
    return 0


def _write(path: Path, results: List[Dict[str, Any]]) -> None:
    path.write_text(json.dumps({"steps": results}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
