#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
报告发布 + 宿主路径卫生闸门。

`reports/` 是**可公开核对**的产物目录（见 reports/README.md），因此从 `.chaos/` 发布过来的
每一份报告都必须先清洗宿主绝对路径。这个工具做两件事：

  1. **发布**（默认）：按 PAIRS 把 `.chaos/*.json` 洗净后写到 `reports/*.json`；
  2. **闸门**（`--check`）：扫描 `reports/*.json`，发现宿主身份路径即 **exit 1**
     —— 它是 CI/发版前可以直接挂上去的那种检查，而不是"靠人记得看一眼"。

清洗规则（都是实测踩出来的）：

  · 仓库根        `D:\\05_code\\credit-card-sys-ops`        → `<repo>`
  · 解释器/家目录  `C:\\Users\\<user>\\...\\python.exe`      → `<python>`
  · 家目录前缀    `C:\\Users\\<user>`                       → `<home>`
  · 正/反斜杠两种写法都处理

为什么必须有第 2、3 条：压测命令会被原样记进报告
（`"cmd": "C:\\Users\\<user>\\...\\python.exe <repo>\\tools\\stress_harness.py ..."`），
只替换仓库根会**把用户名和家目录布局一起提交出去**——而 reports/README.md 里
写的是"宿主绝对路径已清洗为 `<repo>` / `<home>`"，那句话必须是真的。

⚠ 检测器只认**盘符限定的用户目录**（`X:\\Users\\<name>`），**不认**裸的 `AppData`：
`<home>\\AppData\\...` 只暴露标准的 Windows 目录名、不含身份信息（已提交的历史报告就是
这个形式）。第一版检测器把裸 `AppData` 也算成泄漏，于是把 21/42 处**误报**成"用户名泄漏"
—— 检测器自己的假阳性比漏洞更难发现，所以这条留在这里当记录。

用法：
  python tools/publish_reports.py                 # 发布 + 全量复检
  python tools/publish_reports.py --check         # 只做卫生闸门（发现泄漏 exit 1）
  python tools/publish_reports.py --scrub FILE...  # 就地把指定报告里的路径换成占位符
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

# ── 需要从 .chaos/ 发布的配对（新增报告时在这里加一行）────────────────────
PAIRS: list[tuple[str, str]] = [
    (".chaos/ontology_ab_replay.json", "reports/oom_evidence_replay.json"),
    (".chaos/oom_counterfactual.json", "reports/oom_counterfactual.json"),
    (".chaos/rca_retest_res_cluster_memory.json", "reports/rca_retest_res_cluster_memory.json"),
    (".chaos/rca_retest_oom_input_fix.json", "reports/rca_retest_oom_input_fix.json"),
    (".chaos/oom_input_fix_ab.json", "reports/oom_input_fix_ab.json"),
    (".chaos/rca_diagnosis_report_postfix.json", "reports/rca_diagnosis_report_postfix.json"),
    (".chaos/e2e_postfix_ab.json", "reports/e2e_postfix_ab.json"),
    (".chaos/host_path_regression_final.json", "reports/host_path_regression.json"),
]

INTERPRETER = re.compile(r"[A-Za-z]:\\+Users\\+[^\"'\\]*?(?:\\[^\"'\\]*?)*?python\.exe")
INTERPRETER_FWD = re.compile(r"[A-Za-z]:/+Users/+[^\"'/]*(?:/[^\"'/]*)*?python\.exe")
HOME = re.compile(r"[A-Za-z]:\\+Users\\+[^\"'\\]+")
HOME_FWD = re.compile(r"[A-Za-z]:/+Users/+[^\"'/]+")
#: 真正的身份泄漏：盘符限定的用户目录。刻意不含裸 AppData（见模块 docstring 的误报记录）
IDENTITY_LEAK = re.compile(r"[A-Za-z]:\\+Users|[A-Za-z]:/+Users")


def clean_str(s: str) -> str:
    """把一处字符串里的宿主绝对路径换成占位符。"""
    s = s.replace(str(ROOT), "<repo>")
    s = s.replace(str(ROOT).replace("\\", "/"), "<repo>")
    s = INTERPRETER.sub("<python>", s)
    s = INTERPRETER_FWD.sub("<python>", s)
    s = HOME.sub("<home>", s)
    s = HOME_FWD.sub("<home>", s)
    s = re.sub(r"[A-Za-z]:\\+[^\"'\s]*credit-card-sys-ops", "<repo>", s)
    return s


def clean(o: Any) -> Any:
    if isinstance(o, str):
        return clean_str(o)
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o


def leaks(text: str) -> int:
    """统计宿主身份路径（`X:\\Users\\<name>`）的出现次数。"""
    return len(IDENTITY_LEAK.findall(text))


def cmd_publish() -> int:
    missing = 0
    for src, dst in PAIRS:
        s = ROOT / src
        if not s.is_file():
            print("skip     %-46s （源不存在: %s）" % (dst, src))
            missing += 1
            continue
        d = json.loads(s.read_text(encoding="utf-8"))
        out = ROOT / dst
        out.write_text(json.dumps(clean(d), ensure_ascii=False, indent=2), encoding="utf-8")
        print("publish  %-46s %8d bytes  leaks=%d"
              % (dst, out.stat().st_size, leaks(out.read_text(encoding="utf-8"))))
    return 0 if missing == 0 else 0


def cmd_scrub(files: list[str]) -> int:
    """
    就地清洗指定报告。

    必须走 **decode → 替换 → 再编码**，不能在原始 JSON 文本上直接正则替换：
    文件里的路径是**转义过的**（`C:\\\\Users\\\\...`），按单反斜杠写的规则匹配不到。
    （历史报告用 json.dumps(indent=2, ensure_ascii=False) 重排与原文逐字节一致，
    因此只改路径字符串，不动测量值、也不产生格式抖动。）
    """
    for rel in files:
        p = Path(rel)
        if not p.is_absolute():
            p = ROOT / rel
        if not p.is_file():
            print("scrub    %-46s 不存在" % rel, file=sys.stderr)
            return 2
        raw = p.read_text(encoding="utf-8")
        trailing = "\n" if raw.endswith("\n") else ""
        scrubbed = json.dumps(clean(json.loads(raw)), ensure_ascii=False, indent=2) + trailing
        if scrubbed != raw:
            p.write_text(scrubbed, encoding="utf-8")
            print("scrub    %-46s leaks %d -> %d" % (rel, leaks(raw), leaks(scrubbed)))
        else:
            print("scrub    %-46s 无需修改（leaks=%d）" % (rel, leaks(raw)))
    return 0


def cmd_check() -> int:
    bad: list[tuple[str, int]] = []
    n = 0
    for p in sorted((ROOT / "reports").glob("*.json")):
        n += 1
        k = leaks(p.read_text(encoding="utf-8"))
        if k:
            bad.append((p.name, k))
    if bad:
        print("FAIL  reports/ 里有宿主身份路径（%d 份）：" % len(bad))
        for name, k in bad:
            print("        %-46s %d 处" % (name, k))
        print("\n修法: python tools/publish_reports.py            # 重新发布 .chaos 产物")
        print("      python tools/publish_reports.py --scrub reports/<file>.json")
        return 1
    print("PASS  reports/ 宿主路径卫生：%d 份报告，0 处身份路径" % n)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="报告发布 + 宿主路径卫生闸门")
    ap.add_argument("--check", action="store_true",
                    help="只做卫生闸门（发现宿主身份路径即 exit 1）")
    ap.add_argument("--scrub", nargs="+", metavar="FILE",
                    help="就地把指定报告里的宿主路径换成占位符")
    args = ap.parse_args(argv)

    if args.check:
        return cmd_check()
    if args.scrub:
        return cmd_scrub(args.scrub)

    rc = cmd_publish()
    print()
    return max(rc, cmd_check())


if __name__ == "__main__":
    raise SystemExit(main())
