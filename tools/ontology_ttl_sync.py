# -*- coding: utf-8 -*-
"""
TTL ↔ EvoOntology **双向映射**：把"单向生成"补成可对比、可回写的闭环。

## 现状与问题

`ontology_turtle/` 是**单一事实源**（OWL/TTL，人工撰写、可评审），
`.evoontology/` 是运行时本体（5 族记录，供引擎与 Agent 检索）。
但两者之间只有**单向**：`tools/init_evo_ontology.py` 按映射表把 TTL 生成成 v0，
之后 5 轮自演化全在运行时侧累积，**TTL 一直没跟上** —— 于是：

· 运行时新增的概念（`rc:`/`metric:` 等派生术语、以及 v1~v6 的补充）在 TTL 里查不到；
· 运行时侧的扩展字段（`scoring_keywords` / `negative_keywords` / `lifecycle` /
  `remediation` / `role`）**在 TTL 里没有任何表示** —— 它们是"只存在于运行时的事实"，
  而单一事实源原则要求它们至少能被回写与评审。

## 这个工具做什么（以及刻意不做什么）

| 能力 | 说明 |
|---|---|
| `report` | 解析 TTL（rdflib）+ 读运行时 5 族，输出三类差异：**TTL 有而运行时有**（源里有、运行时缺）/ **运行时独有**（含派生术语与历轮新增）/ **无法回写的扩展字段**清单 |
| `--emit-ttl <file>` | 把运行时侧的扩展字段导成一个 **TTL 片段**（自定义属性），供人工评审后并入 `ontology_turtle/` |
| `--emit-patch <file>` | 把"TTL 有而运行时无"的概念导成 **5 族 patch**，走版本化轮次补齐 |

**刻意不做**：自动改写 `ontology_turtle/` 或自动发布运行时版本。
单一事实源的修改必须经过评审；运行时变更必须走 EvoOntology 协议与闸门
（这条纪律是前 6 轮的基础，不能让一个同步脚本绕过去）。

用法：
  python tools/ontology_ttl_sync.py
  python tools/ontology_ttl_sync.py --emit-ttl .chaos/ttl_extension_fragment.ttl
  python tools/ontology_ttl_sync.py --emit-patch .chaos/ttl_missing_patch.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

TTL_DIR = ROOT / "ontology_turtle"
#: 运行时侧的"扩展字段"：TTL 里目前没有对应表示
EXTENSION_FIELDS = ("scoring_keywords", "negative_keywords", "lifecycle",
                    "remediation", "role")


def _local(uri: str) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def _norm(s: str) -> str:
    """归一化用于模糊匹配（去大小写/非字母数字）。"""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def parse_ttl() -> Dict[str, Any]:
    """
    用 rdflib 解析 `ontology_turtle/*.ttl`。

    返回 {subjects: {local: {...}}, classes: [...], prefixes: {...}}。
    解析失败**不静默**：把错误记进返回值的 `errors`，让调用方显示出来。
    """
    from rdflib import RDF, RDFS, Graph, OWL, URIRef
    g = Graph()
    errors: List[str] = []
    files = sorted(TTL_DIR.glob("*.ttl"))
    for f in files:
        try:
            g.parse(str(f), format="turtle")
        except Exception as e:  # noqa: BLE001
            errors.append("%s: %s" % (f.name, str(e)[:160]))

    classes: List[str] = []
    subjects: Dict[str, Dict[str, Any]] = {}
    for s in set(g.subjects()):
        if not isinstance(s, URIRef):
            continue
        local = _local(s)
        if local in ("Ontology",) or not local:
            continue
        types = [str(o).split("#")[-1].split("/")[-1] for o in g.objects(s, RDF.type)]
        labels = []
        for o in g.objects(s, RDFS.label):
            labels.append(str(o))
        comments = [str(o) for o in g.objects(s, RDFS.comment)]
        if "Class" in types:
            classes.append(local)
        props = {}
        for p, o in g.predicate_objects(s):
            pl = _local(p)
            if pl in ("label", "comment", "type"):
                continue
            props.setdefault(pl, []).append(str(o))
        subjects[local] = {"types": types, "labels": labels,
                           "comments": comments[:2], "props": props}
    return {"files": [f.name for f in files], "subjects": subjects,
            "classes": sorted(set(classes)), "triples": len(g), "errors": errors}


def load_runtime() -> Dict[str, Any]:
    root = ROOT / ".evoontology"
    act = json.loads((root / "active.json").read_text(encoding="utf-8"))["active_version"]
    vdir = root / "versions" / act
    fams: Dict[str, Any] = {}
    for fam in ("terms", "mappings", "relations", "constraints", "evidence"):
        fams[fam] = json.loads((vdir / (fam + ".json")).read_text(encoding="utf-8"))
    return {"version": act, "families": fams}


def diff(ttl: Dict[str, Any], rt: Dict[str, Any]) -> Dict[str, Any]:
    """比对 TTL 与运行时，输出三类差异。"""
    terms = rt["families"]["terms"]
    subj = ttl["subjects"]

    # TTL 主题的"可匹配键"：local name + label 归一化
    ttl_keys: Dict[str, str] = {}
    for local, info in subj.items():
        ttl_keys[_norm(local)] = local
        for lb in info.get("labels", []):
            base = lb.split("@")[0].strip().strip('"')
            if base:
                ttl_keys.setdefault(_norm(base), local)

    rt_keys: Dict[str, str] = {}
    for t in terms:
        tid = str(t.get("id"))
        rt_keys[_norm(tid.split(":")[-1])] = tid
        if t.get("name"):
            rt_keys.setdefault(_norm(t["name"]), tid)
        for a in (t.get("aliases") or []):
            rt_keys.setdefault(_norm(a), tid)

    # ① TTL 有、运行时缺（源里有但运行时没有对应 Term）
    ttl_only = []
    for k, local in sorted(ttl_keys.items()):
        if k in rt_keys:
            continue
        info = subj[local]
        types = [str(x) for x in (info.get("types") or [])]
        # 只关心"概念个体"：跳过类本身、以及属性（属性不是概念，不需要 Term）。
        # 不这样过滤的话，`:accesses` / `:appName` 这类谓词会全部混进"缺失概念"里，
        # 117 个里九成是噪声 —— 报告就没法看了。
        if "Class" in types or local in ttl["classes"]:
            continue
        if any("Property" in x for x in types):
            continue
        if not types:
            # 无 rdf:type 的主题多半是被引用出的谓词/字面量宿主
            continue
        ttl_only.append({"ttl_subject": local, "types": types,
                         "label": (info.get("labels") or [""])[0].split("@")[0].strip('"')})

    # ② 运行时独有（TTL 里找不到）
    runtime_only = []
    for t in terms:
        tid = str(t.get("id"))
        keys = {_norm(tid.split(":")[-1])}
        if t.get("name"):
            keys.add(_norm(t["name"]))
        for a in (t.get("aliases") or []):
            keys.add(_norm(a))
        if keys & set(ttl_keys):
            continue
        runtime_only.append({"id": tid, "name": t.get("name"),
                             "type": t.get("type"), "scope": t.get("scope")})

    # ③ 无法回写的扩展字段
    ext: List[Dict[str, Any]] = []
    for t in terms:
        have = [f for f in EXTENSION_FIELDS if t.get(f)]
        if have:
            ext.append({"id": t.get("id"), "fields": have,
                        "remediation_actions": len(t.get("remediation") or [])})

    return {
        "ttl_triples": ttl["triples"], "ttl_files": ttl["files"],
        "ttl_classes": len(ttl["classes"]), "ttl_subjects": len(subj),
        "runtime_version": rt["version"],
        "runtime_terms": len(terms),
        "ttl_only": ttl_only,
        "runtime_only": runtime_only,
        "extension_terms": ext,
        "parse_errors": ttl["errors"],
    }


def emit_ttl_fragment(rt: Dict[str, Any], out: Path) -> Path:
    """
    把运行时侧的扩展字段导出成 TTL 片段（自定义属性），供人工评审后并入单一事实源。

    属性命名用项目自身的命名空间（`:scoringKeywords` 等），并显式注明
    "这是运行时侧扩展，需人工确认后再并入 ontology_turtle/"。
    """
    terms = rt["families"]["terms"]
    lines = [
        "# ── 运行时侧扩展字段的 TTL 片段（由 tools/ontology_ttl_sync.py 生成）──",
        "# 用途：把只存在于 .evoontology/ 的事实（打分关键词/负向词/生命周期/处置动作）",
        "# 回写成 TTL，供人工评审后并入 ontology_turtle/ —— 单一事实源不应长期落后于运行时。",
        "# 注意：本体/属性命名以人工评审为准；本文件只是**提案**。",
        "",
        "@prefix : <https://sapiens.ai/ontology/credit-card-ops#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "",
    ]
    n = 0
    for t in sorted(terms, key=lambda x: str(x.get("id"))):
        tid = str(t.get("id"))
        local = re.sub(r"[^A-Za-z0-9]", "_", tid)
        blocks = []
        if t.get("scoring_keywords"):
            blocks.append('  :scoringKeywords "%s" .'
                          % " , ".join(str(x) for x in t["scoring_keywords"][:12]))
        if t.get("negative_keywords"):
            blocks.append('  :negativeKeywords "%s" .'
                          % " , ".join(str(x) for x in t["negative_keywords"][:8]))
        if t.get("lifecycle"):
            blocks.append('  :lifecycleState "%s" .' % (t["lifecycle"] or {}).get("state"))
        if t.get("role"):
            blocks.append('  :rcaRole "%s" .' % t["role"])
        if t.get("remediation"):
            for a in t["remediation"][:8]:
                blocks.append('  :remediationAction "%s | %s | %s" .'
                              % (a.get("urgency"), str(a.get("action"))[:60],
                                 str(a.get("command"))[:80]))
        if not blocks:
            continue
        n += 1
        lines.append(":%s a :RuntimeExtension ;" % local)
        lines.append('  rdfs:label "%s" ;' % tid)
        lines.append(" ;\n".join(blocks) + " .")
        lines.append("")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("  已导出 TTL 片段：%s（%d 个术语）" % (out, n))
    return out


def emit_missing_patch(d: Dict[str, Any], out: Path) -> Path:
    """把"TTL 有而运行时无"的概念导成 5 族 patch（供版本化轮次补齐）。"""
    patch: Dict[str, List[Dict[str, Any]]] = {"terms": [], "mappings": [],
                                             "relations": [], "constraints": [],
                                             "evidence": []}
    for item in d["ttl_only"]:
        local = item["ttl_subject"]
        label = item.get("label") or local
        patch["terms"].append({
            "id": "ext:%s" % local.lower(),
            "name": label,
            "type": "entity",
            "scope": "待定（由 TTL 概念推导）",
            "definition": "由 TTL 单一事实源补入：TTL 里存在 `:%s`，运行时尚无对应 Term。" % local,
            "aliases": [local],
            "evidence_refs": [],
            "lifecycle": {"state": "draft"},   # 未接地前一律 draft
        })
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  已导出待补 patch：%s（%d 个概念，均标 draft）" % (out, len(patch["terms"])))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="TTL ↔ EvoOntology 双向映射（报告 + 提案）")
    ap.add_argument("--emit-ttl", default=None, help="导出运行时扩展字段的 TTL 片段")
    ap.add_argument("--emit-patch", default=None, help="导出 TTL-only 概念的 5 族 patch")
    ap.add_argument("--json-out", default=str(ROOT / ".chaos" / "ontology_ttl_sync.json"))
    args = ap.parse_args(argv)

    print("=" * 92)
    print("TTL ↔ EvoOntology 双向映射")
    print("=" * 92)
    ttl = parse_ttl()
    rt = load_runtime()
    for e in ttl["errors"]:
        print("  ⚠ TTL 解析问题：%s" % e)

    d = diff(ttl, rt)
    print("  TTL：%d 个文件 / %d 三元组 / %d 个类 / %d 个主题"
          % (len(d["ttl_files"]), d["ttl_triples"], d["ttl_classes"], d["ttl_subjects"]))
    print("  运行时：%s / %d 个术语" % (d["runtime_version"], d["runtime_terms"]))
    print()
    print("  ① TTL 有而**运行时没有**的概念：%d 个" % len(d["ttl_only"]))
    for x in d["ttl_only"][:10]:
        print("     :%-30s %s" % (x["ttl_subject"][:30], str(x.get("label"))[:40]))
    print()
    print("  ② **运行时独有**（TTL 里查不到）：%d 个 —— 历轮演化与派生术语都在这里"
          % len(d["runtime_only"]))
    by_prefix: Dict[str, int] = {}
    for x in d["runtime_only"]:
        by_prefix[str(x["id"]).split(":")[0]] = by_prefix.get(str(x["id"]).split(":")[0], 0) + 1
    print("     按前缀分布 =", by_prefix)
    for x in d["runtime_only"][:8]:
        print("     %-30s %s" % (x["id"], str(x.get("name"))[:40]))
    print()
    print("  ③ **无法回写的扩展字段**（TTL 里没有任何表示）：%d 个术语" % len(d["extension_terms"]))
    field_count: Dict[str, int] = {}
    for x in d["extension_terms"]:
        for f in x["fields"]:
            field_count[f] = field_count.get(f, 0) + 1
    print("     字段覆盖 =", field_count)

    if args.emit_ttl:
        emit_ttl_fragment(rt, Path(args.emit_ttl))
    if args.emit_patch:
        emit_missing_patch(d, Path(args.emit_patch))

    Path(args.json_out).write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print("\n报告: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
