"""Protocol checks for the dependency-free semantic MCP server."""

import json
import os
import subprocess
import sys
from pathlib import Path

from evoontology import SemanticStore


def test_stdio_mcp_initialize_and_list_tools(tmp_path):
    workspace = tmp_path / ".evoontology"
    SemanticStore.save_version(
        workspace,
        "ontology_v0",
        {family: [] for family in ("terms", "mappings", "relations", "constraints", "evidence")},
    )
    SemanticStore.set_active(workspace, "ontology_v0")

    requests = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            ),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "",
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evoontology.runtime.mcp_server",
            "--store",
            str(workspace),
        ],
        input=requests,
        text=True,
        capture_output=True,
        check=True,
    )

    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    tool_names = [tool["name"] for tool in responses[1]["result"]["tools"]]
    assert tool_names[:2] == ["browse_semantics", "resolve_semantics"]
    assert "validate_semantics" in tool_names
    assert "accept_evolution" in tool_names


def test_resources_templates_list_is_empty(tmp_path):
    workspace = tmp_path / ".evoontology"
    requests = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"}) + "\n"
    completed = subprocess.run(
        [sys.executable, "-m", "evoontology.runtime.mcp_server", "--store", str(workspace)],
        input=requests,
        text=True,
        capture_output=True,
        check=True,
    )
    response = json.loads(completed.stdout.splitlines()[0])
    assert response["result"] == {"resourceTemplates": []}


def test_parse_error_reports_null_id_and_keeps_serving(tmp_path):
    """A malformed line must never poison the id of a later request."""
    workspace = tmp_path / ".evoontology"
    lines = [
        "{this is not json",
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}),
        "",
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "evoontology.runtime.mcp_server", "--store", str(workspace)],
        input="\n".join(lines),
        text=True,
        capture_output=True,
        check=True,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 2
    assert responses[0]["id"] is None
    assert responses[0]["error"]["code"] == -32700
    assert responses[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_chinese_arguments_survive_non_utf8_host_locale(tmp_path):
    """Requests carrying UTF-8 CJK text must get a real reply even when the
    server inherits a legacy console code page (no PYTHONIOENCODING/PYTHONUTF8).

    Regression: Codex spawns plugin MCP servers with the ambient Windows
    environment, where stdin/stdout default to the console code page (GBK on
    zh-CN hosts). The garbled decode used to drop requests silently, leaving
    the client waiting until its 300s timeout.
    """
    workspace = tmp_path / "语义工作区" / ".evoontology"
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_versions", "arguments": {"workspace": str(workspace)}},
        },
        ensure_ascii=False,
    )
    # Run by file path (as the plugin launcher does): this must work from a
    # cwd that does not contain the evoontology package.
    server_script = Path(__file__).resolve().parents[1] / "evoontology" / "runtime" / "mcp_server.py"
    proc = subprocess.Popen(
        [sys.executable, str(server_script)],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(
            (request + "\n").encode("utf-8"), timeout=30
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("server never answered a UTF-8 CJK request")
    responses = [json.loads(line) for line in stdout.decode("utf-8").splitlines() if line.strip()]
    assert len(responses) == 1, stderr.decode("utf-8", "replace")
    payload = responses[0]
    assert payload["id"] == 3
    assert "result" in payload, payload
    text = payload["result"]["content"][0]["text"]
    assert json.loads(text)["status"] == "ok"
