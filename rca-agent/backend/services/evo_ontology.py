# -*- coding: utf-8 -*-
"""EvoOntology MCP 客户端封装."""
from __future__ import annotations
import json, subprocess, os, sys, time
from typing import Any, Dict, List, Optional
from pathlib import Path

# ── 拓扑缓存（进程内；本体版本变化自动失效）────────────────────────────────
_TOPOLOGY_CACHE: Dict[str, Dict[str, Any]] = {}
_TOPOLOGY_TTL_S = 600.0


class EvoOntologyClient:
    """
    通过 stdio JSON-RPC 调用 EvoOntology MCP Server。

    工作原理：
    1. 启动子进程 python -m evoontology.runtime.mcp_server --store <workspace>
    2. 发送 initialize + tools/call JSON-RPC 请求
    3. 解析响应（content[0].text 内层 JSON）
    """

    def __init__(self, workspace: Optional[str] = None):
        # 项目根目录（本地开发：rca-agent/../ 即项目根）
        project_root = Path(__file__).parent.parent.parent.parent

        # ── 定位 EvoOntology vendor 包 ──────────────────────────────
        # 候选顺序：环境变量 > /app/vendor > <project>/vendor > 已安装的包
        vendor_candidates = [
            os.environ.get("EVO_VENDOR", ""),
            "/app/vendor/EvoOntology",
            str(project_root / "vendor" / "EvoOntology"),
            str(Path(__file__).parent.parent.parent / "vendor" / "EvoOntology"),
        ]
        self.evo_path = ""
        for cand in vendor_candidates:
            if cand and Path(cand).is_dir():
                self.evo_path = str(Path(cand).resolve())
                break
        if not self.evo_path:
            # 回退：如果 evoontology 已作为包安装，用其父目录
            try:
                import evoontology
                self.evo_path = str(Path(evoontology.__file__).parent.parent.resolve())
            except Exception:
                self.evo_path = str(project_root / "vendor" / "EvoOntology")

        # ── 定位本体工作区 ──────────────────────────────────────────
        if workspace:
            self.workspace = workspace
        else:
            ws_candidates = [
                os.environ.get("EVO_WORKSPACE", ""),
                "/app/.evoontology",
                str(project_root / ".evoontology"),
            ]
            self.workspace = str(project_root / ".evoontology")
            for cand in ws_candidates:
                if cand and Path(cand).is_dir():
                    self.workspace = str(Path(cand).resolve())
                    break

        self.python_exe = self._find_python()

    def _find_python(self) -> str:
        """查找可用的 Python 解释器。"""
        import shutil
        candidates = [
            os.environ.get("PYTHON_EXE", ""),
            # 当前运行解释器（最可靠）
            sys.executable,
            # 本地 venv（Windows / Linux）
            str(Path(self.evo_path) / ".venv" / "Scripts" / "python.exe"),
            str(Path(self.evo_path) / ".venv" / "bin" / "python"),
            "python3",
            "python",
        ]
        for c in candidates:
            if not c:
                continue
            c_clean = str(c).replace('"', "")
            if os.path.isabs(c_clean):
                if Path(c_clean).exists():
                    return c_clean
            else:
                found = shutil.which(c_clean)
                if found:
                    return found
        return sys.executable

    def _env(self) -> Dict[str, str]:
        env = {}
        for k, v in os.environ.items():
            if any(s in k.upper() for s in ["KEY", "PASSWORD", "SECRET", "TOKEN"]) and "PYTHON" not in k.upper():
                continue
            env[k] = v
        env["PYTHONPATH"] = self.evo_path
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """发送 JSON-RPC 请求并返回解析后的结果。"""
        init = json.dumps({
            "jsonrpc": "2.0", "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"}
        }).encode("utf-8")

        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}
        }).encode("utf-8")

        proc = subprocess.Popen(
            [self.python_exe, "-m", "evoontology.runtime.mcp_server",
             "--store", self.workspace],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=self._env()
        )
        proc.stdin.write(init + b"\n")
        proc.stdin.write(payload + b"\n")
        proc.stdin.close()
        out, _ = proc.communicate(timeout=60)

        for line in out.decode("utf-8", errors="replace").strip().split("\n"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if r.get("id") == 1:
                    result = r.get("result", {})
                    # MCP content[0].text 内层 JSON
                    content = result.get("content", [])
                    if content and isinstance(content, list) and len(content) > 0:
                        raw_text = content[0].get("text", "{}")
                        try:
                            return json.loads(raw_text)
                        except json.JSONDecodeError:
                            return {"raw": raw_text, "result": result}
                    return result
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return {}

    # ── MCP 工具封装 ──────────────────────────────────────────────────────────

    def browse_semantics(self, query: str, kind: str = "all", limit: int = 30) -> Dict[str, Any]:
        """搜索本体语义。"""
        return self._call("browse_semantics", {
            "query": query, "workspace": self.workspace, "kind": kind, "limit": limit
        })

    def resolve_semantics(self, mentions: List[str], context: str = "") -> Dict[str, Any]:
        """解析术语并返回关联的边（关系/约束/证据）。"""
        return self._call("resolve_semantics", {
            "mentions": mentions, "context": context, "workspace": self.workspace
        })

    def validate_semantics(self, workspace: Optional[str] = None) -> Dict[str, Any]:
        """验证本体工作区。"""
        return self._call("validate_semantics", {
            "workspace": workspace or self.workspace
        })

    def evolution_status(self) -> Dict[str, Any]:
        """查询自演化状态。"""
        return self._call("evolution_status", {"workspace": self.workspace})

    def visualize_ontology(self, version: Optional[str] = None,
                           open_browser: bool = False) -> Dict[str, Any]:
        """生成可视化 HTML。"""
        args = {"workspace": self.workspace, "open_browser": open_browser}
        if version:
            args["version"] = version
        return self._call("visualize_ontology", args)

    # ── 高级封装 ──────────────────────────────────────────────────────────────

    def _term_meta(self, term_id: str) -> Dict[str, str]:
        """从本体的 terms.json 读取术语元数据（name/type/scope/definition）。"""
        try:
            workspace_path = Path(self.workspace)
            active_file = workspace_path / "active.json"
            if not active_file.exists():
                return {}
            active_data = json.loads(active_file.read_text(encoding="utf-8"))
            version = active_data.get("active_version") or active_data.get("version") or "ontology_v0"
            terms_file = workspace_path / "versions" / version / "terms.json"
            if not terms_file.exists():
                return {}
            for t in json.loads(terms_file.read_text(encoding="utf-8")):
                if t.get("id") == term_id:
                    return {
                        "name": str(t.get("name", "")),
                        "type": str(t.get("type", "")),
                        "scope": str(t.get("scope", "")),
                        "definition": str(t.get("definition", "")),
                    }
        except Exception:
            pass
        return {}

    def get_topology_graph(self, app_name: str = "payment-app",
                           max_nodes: int = 160, max_depth: int = 6,
                           use_cache: bool = True) -> Dict[str, Any]:
        """
        从应用术语出发做 BFS 图遍历，返回完整拓扑 {nodes, edges}。

        遍历上限随本体规模上调：本体从 18 个 Term（单实例视图）演进到
        51 个 Term（集群视图）后，原来 40 节点 / 4 跳的上限会把
        `rc:row-lock`、`rc:tmp-disk` 这类**深度较靠后的根因术语直接截掉**，
        表现为"线上快路径候选集里根本没有正确的根因" ——
        离线 A/B（读文件、不设上限）能到 20/21，线上却只有 15/21，差距全部来自这里。

        拓扑变化频率极低（仅在本体演进时改变），而 BFS 需要多次 MCP
        子进程往返。因此默认启用进程内缓存，缓存键包含当前 active 版本号
        + 本体目录 mtime，本体更新后自动失效。
        """
        if use_cache:
            cached = self._topology_cache_get(app_name)
            if cached is not None:
                return cached

        result = self._topology_bfs(app_name, max_nodes, max_depth)
        if use_cache:
            self._topology_cache_put(app_name, result)
        return result

    # ── 拓扑缓存 ──────────────────────────────────────────────────────

    def _cache_key(self, app_name: str) -> str:
        """缓存键 = app + active 版本 + 版本目录 mtime。"""
        try:
            ws = Path(self.workspace)
            active = ws / "active.json"
            version = ""
            if active.is_file():
                data = json.loads(active.read_text(encoding="utf-8"))
                version = data.get("active_version") or data.get("version") or ""
            vdir = ws / "versions" / version if version else ws
            mtime = int(vdir.stat().st_mtime) if vdir.exists() else 0
            return "%s|%s|%d" % (app_name, version, mtime)
        except Exception:
            return "%s|unknown" % app_name

    def _topology_cache_get(self, app_name: str) -> Optional[Dict[str, Any]]:
        key = self._cache_key(app_name)
        cached = _TOPOLOGY_CACHE.get(key)
        if cached is None:
            return None
        age = time.time() - cached["at"]
        if age > _TOPOLOGY_TTL_S:
            _TOPOLOGY_CACHE.pop(key, None)
            return None
        return cached["graph"]

    def _topology_cache_put(self, app_name: str, graph: Dict[str, Any]) -> None:
        _TOPOLOGY_CACHE[self._cache_key(app_name)] = {
            "at": time.time(), "graph": graph,
        }
        # 简单淘汰，避免无界增长
        if len(_TOPOLOGY_CACHE) > 8:
            oldest = min(_TOPOLOGY_CACHE.items(), key=lambda kv: kv[1]["at"])
            _TOPOLOGY_CACHE.pop(oldest[0], None)

    def invalidate_topology_cache(self) -> None:
        _TOPOLOGY_CACHE.clear()

    def _topology_bfs(self, app_name: str,
                      max_nodes: int, max_depth: int) -> Dict[str, Any]:
        """实际执行 BFS 遍历（不含缓存）。

        遍历策略:
          1. 种子 = 匹配 app_name 的术语 id
          2. 对每个节点调用 resolve_semantics 展开其关系边
          3. 边的对端加入队列，继续展开，直到无新节点或达到上限
        """
        seen: Dict[str, Dict[str, Any]] = {}
        edges: Dict[str, Dict[str, Any]] = {}

        # ── 定位种子术语 ──────────────────────────────────────────────
        seeds = []
        try:
            ws = Path(self.workspace)
            active_file = ws / "active.json"
            if active_file.exists():
                active_data = json.loads(active_file.read_text(encoding="utf-8"))
                version = active_data.get("active_version") or "ontology_v0"
                terms_file = ws / "versions" / version / "terms.json"
                if terms_file.exists():
                    for t in json.loads(terms_file.read_text(encoding="utf-8")):
                        tid = str(t.get("id", ""))
                        name = str(t.get("name", ""))
                        if app_name.lower() in tid.lower() or app_name.lower() in name.lower():
                            seeds.append(tid)
        except Exception:
            pass

        if not seeds:
            # 回退到 browse_semantics
            raw = self.browse_semantics(app_name, kind="all", limit=10)
            seeds = [str(i.get("id")) for i in raw.get("items", []) if str(i.get("type")) == "entity"]

        seeds = list(dict.fromkeys(seeds))[:5]
        if not seeds:
            return {"nodes": [], "edges": []}

        # ── BFS ──────────────────────────────────────────────────────
        queue = [(s, 0) for s in seeds]
        visited_depth: Dict[str, int] = {}

        while queue:
            term_id, depth = queue.pop(0)
            if term_id in seen or depth > max_depth or len(seen) >= max_nodes:
                continue

            meta = self._term_meta(term_id)
            seen[term_id] = {
                "id": term_id,
                "name": meta.get("name", term_id),
                "type": meta.get("type", "entity"),
                "scope": meta.get("scope", ""),
                "definition": meta.get("definition", ""),
            }

            # 展开关系
            try:
                res = self.resolve_semantics([term_id], context="topology BFS")
            except Exception:
                continue

            for item in (res.get("results") or []):
                if not isinstance(item, dict):
                    continue
                # 用请求的 term_id 作为边的主体（解析可能返回 None）
                for rel in (item.get("relations") or []):
                    if not isinstance(rel, dict):
                        continue
                    rid = str(rel.get("id", ""))
                    src = str(rel.get("source") or term_id)
                    tgt = str(rel.get("target") or "")
                    rtype = str(rel.get("relation_type", ""))
                    cond = str(rel.get("connection_condition", ""))
                    if not rid or not tgt:
                        continue
                    if rid not in edges:
                        edges[rid] = {
                            "id": rid,
                            "source": src,
                            "target": tgt,
                            "relation_type": rtype,
                            "condition": cond,
                        }
                    # 对端节点入队
                    if tgt not in seen and tgt not in visited_depth:
                        visited_depth[tgt] = depth + 1
                        queue.append((tgt, depth + 1))
                    # 反向也探索（有些关系只有单向声明）
                    if src and src != term_id and src not in seen and src not in visited_depth:
                        visited_depth[src] = depth + 1
                        queue.append((src, depth + 1))

        # 只为已收集到的节点保留边
        node_ids = set(seen.keys())
        final_edges = [e for e in edges.values() if e["source"] in node_ids and e["target"] in node_ids]

        return {"nodes": list(seen.values()), "edges": final_edges}

    def get_topology(self, app_name: str = "payment-app") -> List[Dict[str, Any]]:
        """获取 app 拓扑节点列表（兼容旧接口）。"""
        return self.get_topology_graph(app_name).get("nodes", [])

    def get_term_relations(self, term_id: str) -> Dict[str, Any]:
        """获取指定 Term 的所有关联关系。"""
        return self.resolve_semantics([term_id], context="RCA topology traversal")

    def get_all_evidences(self) -> List[Dict[str, Any]]:
        """获取所有 Evidence 记录（从 validate 结果或直接读文件）。"""
        workspace_path = Path(self.workspace)
        active_file = workspace_path / "active.json"
        if not active_file.exists():
            return []
        import json as _json
        active_data = _json.loads(active_file.read_text(encoding="utf-8"))
        version = active_data.get("version", "ontology_v0")
        version_dir = workspace_path / "versions" / version
        ev_file = version_dir / "evidence.json"
        if not ev_file.exists():
            return []
        return _json.loads(ev_file.read_text(encoding="utf-8"))
