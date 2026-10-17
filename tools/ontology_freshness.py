#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
本体证据时效检查（evidence freshness）—— 本体里点名的环境实体，现在还存在吗？

## 为什么需要

`ontology_v6` 里有 4 条证据、1 条映射、2 个术语仍然点名单实例时代的容器
`cc-credit-card-app`；集群栈里早就没有这个容器了（它只在 demo/ 的单机栈里）。
`466e77e` 修过智能体工具里的同类问题，本体这边却没人发现 —— 因为**没有任何检查
会因为"证据描述的环境已经不在了"而变红**。证据有时间戳，但时间戳只记录"何时看到"，
不说明"现在是否还成立"。这与 docs/lessons/06 是同一类：证据和它描述的现实脱钩了。

## 为什么不是"重新执行 evidence.query"

22 条证据的 `query` 是**自由文本**：docker CLI、HTTP、SQL、PromQL、PowerShell、
Python 表达式、协议调用名混在一起。把本体数据当 shell 命令执行就是命令注入面；
而且相当一部分根本不可执行。所以这里检查的是**可判定的那部分**：
记录里出现的容器名 / 主机名，是否属于声明的环境（`rca-agent/docker-compose.yml`）。
它离线、确定、可进 CI；重新观测证据内容需要集群在线，另行处理。

## 规则

| 规则 | 范围 | 级别 |
|---|---|---|
| F1 | 描述"当前状态"的记录（terms / mappings / relations / constraints）不得点名声明环境之外的实体 | 违反 |
| F2 | 未标 `status: historical` 的证据不得点名声明环境之外的实体 | 违反 |
| F2b | 标为 historical 的证据必须写明 `observed_in` 与 `historical_reason` | 违反 |
| F3 | 生效中的记录，其 evidence_refs **全部**是历史证据 → 待重新观测 | 告警 |

历史证据是真实的过去观测，**不改写其内容**；改写一条没重新观测过的证据等于伪造。
F3 只告警不阻断：离线环境无法产生新观测，阻断只会逼人编证据。

用法：
  python tools/ontology_freshness.py                    # 检查激活版本
  python tools/ontology_freshness.py --version ontology_v6
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / ".evoontology"
COMPOSE = ROOT / "rca-agent" / "docker-compose.yml"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")   # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

#: 形如容器名的标识符。前后不能紧贴路径分隔符：`rca-agent/backend/...` 是路径，不是容器。
CONTAINER_TOKEN = re.compile(r"(?<![\w/.-])((?:cc|rca-agent)(?:-[a-z0-9]+)+)(?![\w/])")
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
LOOPBACK = frozenset({"localhost", "127.0.0.1"})

CURRENT_STATE_FAMILIES = ("terms", "mappings", "relations", "constraints")
HISTORICAL = "historical"


@dataclass(frozen=True)
class DeclaredEnv:
    """声明的环境：显式 container_name、服务名（容器网络里的 DNS 名）、以及 compose 生成的副本名前缀。"""
    names: FrozenSet[str]
    replica_prefixes: FrozenSet[str]

    def declares(self, token: str) -> bool:
        if token in self.names or token in LOOPBACK:
            return True
        for prefix in self.replica_prefixes:
            # `rca-agent-payment-app-2`，或文中的模板写法 `rca-agent-payment-app`（后接 -N）
            if token == prefix.rstrip("-") or re.fullmatch(re.escape(prefix) + r"\d+", token):
                return True
        # 编号容器的组前缀：`cc-mysql-replica` 指 cc-mysql-replica-1/2
        return any(re.fullmatch(re.escape(token) + r"-\d+", n) for n in self.names)


def declared_environment(compose_path: Path = COMPOSE) -> DeclaredEnv:
    spec = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    project = spec.get("name") or compose_path.parent.name
    names, prefixes = set(), set()
    for service, cfg in (spec.get("services") or {}).items():
        names.add(service)
        cname = (cfg or {}).get("container_name")
        if cname:
            names.add(cname)
        else:
            prefixes.add("%s-%s-" % (project, service))
    return DeclaredEnv(frozenset(names), frozenset(prefixes))


def _strings(obj: Any, path: str = "") -> Iterator[tuple]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, "%s.%s" % (path, k) if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, "%s[%d]" % (path, i))


def undeclared_refs(record: Dict[str, Any], env: DeclaredEnv) -> List[Dict[str, str]]:
    out = []
    for field, text in _strings(record):
        tokens = CONTAINER_TOKEN.findall(text) + URL_HOST.findall(text)
        for tok in tokens:
            if not env.declares(tok):
                out.append({"field": field, "token": tok})
    return out


def is_historical(evidence: Dict[str, Any]) -> bool:
    return str(evidence.get("status") or "") == HISTORICAL


def check_records(rec: Dict[str, List[dict]], env: DeclaredEnv | None = None) -> Dict[str, Any]:
    env = env or declared_environment()
    violations: List[Dict[str, Any]] = []
    for fam in CURRENT_STATE_FAMILIES:
        for r in rec.get(fam) or []:
            for ref in undeclared_refs(r, env):
                violations.append({"rule": "F1", "family": fam, "id": r.get("id"), **ref})
    evidence = {e.get("id"): e for e in rec.get("evidence") or []}
    for eid, e in evidence.items():
        if is_historical(e):
            missing = [k for k in ("observed_in", "historical_reason") if not e.get(k)]
            if missing:
                violations.append({"rule": "F2b", "family": "evidence", "id": eid,
                                   "field": ",".join(missing), "token": ""})
            continue
        for ref in undeclared_refs(e, env):
            violations.append({"rule": "F2", "family": "evidence", "id": eid, **ref})

    historical = {eid for eid, e in evidence.items() if is_historical(e)}
    needs_reobservation: List[Dict[str, Any]] = []
    for fam in CURRENT_STATE_FAMILIES:
        for r in rec.get(fam) or []:
            state = str((r.get("lifecycle") or {}).get("state") or "active")
            refs = [x for x in (r.get("evidence_refs") or []) if x]
            if state in ("active", "validated") and refs and set(refs) <= historical:
                needs_reobservation.append({"family": fam, "id": r.get("id"), "evidence_refs": refs})

    affected = {(v["family"], v["id"]) for v in violations}
    return {
        "passed": not violations,
        "violations": violations,
        "affected_records": sorted("%s:%s" % k for k in affected),
        "historical_evidence": sorted(historical),
        "needs_reobservation": needs_reobservation,
        "declared_env_size": len(env.names),
    }


def record_scores(rec: Dict[str, List[dict]], env: DeclaredEnv | None = None) -> Dict[str, Any]:
    """逐记录 0/1：该记录没有 F1/F2/F2b 违反即 1。供演化闸门做成对评估。"""
    res = check_records(rec, env)
    bad = set(res["affected_records"])
    case_ids, scores = [], []
    for fam in CURRENT_STATE_FAMILIES + ("evidence",):
        for r in rec.get(fam) or []:
            cid = "%s:%s" % (fam, r.get("id"))
            case_ids.append(cid)
            scores.append(0.0 if cid in bad else 1.0)
    return {"case_ids": case_ids, "scores": scores, "check": res}


def _load(version: str) -> Dict[str, List[dict]]:
    vdir = WORKSPACE / "versions" / version
    out = {}
    for fam in CURRENT_STATE_FAMILIES + ("evidence",):
        data = json.loads((vdir / ("%s.json" % fam)).read_text(encoding="utf-8"))
        out[fam] = data if isinstance(data, list) else data.get("items", data)
    return out


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本体证据时效检查")
    ap.add_argument("--version", default="")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)
    version = args.version or json.loads(
        (WORKSPACE / "active.json").read_text(encoding="utf-8"))["active_version"]
    res = check_records(_load(version))
    print("证据时效检查 %s（声明环境 %d 个名字，来自 %s）"
          % (version, res["declared_env_size"], COMPOSE.relative_to(ROOT)))
    for v in res["violations"]:
        print("  [%s] %s %s  %s → %s" % (v["rule"], v["family"], v["id"], v["field"], v["token"]))
    print("  历史证据 %d 条：%s" % (len(res["historical_evidence"]), res["historical_evidence"]))
    print("  待重新观测（只靠历史证据支撑）%d 条" % len(res["needs_reobservation"]))
    for n in res["needs_reobservation"]:
        print("    - %s %s" % (n["family"], n["id"]))
    print("  结论: %s" % ("PASS" if res["passed"] else "FAIL（%d 条违反）" % len(res["violations"])))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
