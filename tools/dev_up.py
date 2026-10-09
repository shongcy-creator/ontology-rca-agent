# -*- coding: utf-8 -*-
"""
一条命令把环境带到"可用"状态：起栈 → 引导（schema + 复制）→ 建数据集 → 体检。

## 为什么要有它

"引导集群"这一步（`cluster_bootstrap.py`：建 schema + `CHANGE REPLICATION SOURCE TO ... START REPLICA`）
**不在 compose 里**，而副本容器默认是**空实例**（没有 appuser、没有表、复制为空）。
于是"起环境"其实是 4 件事，却散落在**文档 / CI / compose** 三处 —— 实测被漏掉过三次：

  1. CI workflow 漏了它        → 读路径 Access denied + 两条 replication FAIL
  2. Linux 迁移文档漏了它      → 照文档从零部署会踩同一个坑
  3. 英文 README 的 Quickstart 漏了它 → 同上

漏掉的后果很隐蔽：**所有容器都 healthy**，只有 doctor 能看出来 —— 也就是说
"照文档部署"这条路当时是走不通的，而表面上一切正常。

修法不是"在三处各补一句话"（那只治症状），而是**收敛成一个入口**：本脚本。
文档、CI、开发流程都只调它，就不可能再漏。

用法：
  python tools/dev_up.py                 # 全流程（推荐）
  python tools/dev_up.py --build         # 顺带重建镜像（首次或改了代码）
  python tools/dev_up.py --skip-seed     # 已有数据集，跳过（省时间）
  python tools/dev_up.py --doctor-retries 15   # 慢机器上多等一会儿（默认 8）
  python tools/dev_up.py --check         # 只看当前状态，不做任何改动
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", str(ROOT / "rca-agent" / "docker-compose.yml")]
PY = sys.executable

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


def run(cmd, label, timeout=None):
    print("\n" + "=" * 92)
    print("[%s] $ %s" % (label, " ".join(str(c) for c in cmd[1:])))
    print("=" * 92, flush=True)
    return subprocess.run([str(c) for c in cmd], cwd=str(ROOT), timeout=timeout).returncode


def doctor_ok():
    p = subprocess.run([PY, "tools/fault_injector.py", "doctor"], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一条命令把环境带到可用状态")
    ap.add_argument("--build", action="store_true", help="重建镜像")
    ap.add_argument("--skip-up", action="store_true", help="不起栈（已在运行）")
    ap.add_argument("--skip-bootstrap", action="store_true")
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--doctor-retries", type=int, default=8,
                    help="体检重试次数（每次都打印进度；慢机器上调大）")
    ap.add_argument("--check", action="store_true", help="只体检，不做任何改动")
    a = ap.parse_args(argv)

    print("=" * 92)
    print("一键起环境：起栈 → 引导(schema+复制) → seed → 体检")
    print("=" * 92)

    if a.check:
        ok, out = doctor_ok()
        print(out[-4000:])
        return 0 if ok else 1

    if not a.skip_up:
        cmd = COMPOSE + ["up", "-d"] + (["--build"] if a.build else [])
        if run(cmd, "1/4 起栈") != 0:
            print("✘ compose up 失败")
            return 2

    if not a.skip_bootstrap:
        # 这一步不在 compose 里。幂等：重复执行只是再次确保复制在跑。
        if run([PY, "tools/cluster_bootstrap.py"], "2/4 引导集群（schema + 复制）",
               timeout=900) != 0:
            print("✘ cluster_bootstrap 失败 —— 副本会是空实例（无 appuser/无表/无复制），"
                  "体检会在「读路径」与「replication」两项上报错")
            return 3
    else:
        print("\n[2/4] 引导集群 ... 跳过（--skip-bootstrap）")

    if not a.skip_seed:
        if run([PY, "tools/fault_injector.py", "seed"], "3/4 建数据集（约 480 万行）",
               timeout=3600) != 0:
            print("✘ seed 失败")
            return 4
    else:
        print("\n[3/4] 建数据集 ... 跳过（--skip-seed）")

    print("\n" + "=" * 92)
    print("[4/4] 环境体检（18 项；最多重试 %d 次）" % a.doctor_retries)
    print("=" * 92, flush=True)
    for i in range(1, a.doctor_retries + 1):
        ok, out = doctor_ok()
        print(out[-4000:], flush=True)
        if ok:
            print("\n✅ 环境就绪（第 %d 次体检通过）。控制台：http://localhost:3001" % i)
            return 0
        if i < a.doctor_retries:
            print("… 第 %d 次未通过，20s 后重试（Prometheus/blackbox/复制可能需要时间收敛）"
                  % i, flush=True)
            time.sleep(20)
    print("\n✘ 体检仍未通过 —— 上面每一项的 FAIL 行都直接说明了缺什么")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
