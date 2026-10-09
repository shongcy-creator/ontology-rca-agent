"""Implementation for the insightbench.insightbench.tceo.tool_chat module."""

import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from openai import OpenAI

from insightbench.tceo.retriever import InsightSemanticLayer

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
EXECUTE_PYTHON_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_python",
        "description": (
            "Execute Python code in a sandbox to analyze data, compute statistics, "
            "and generate plots. The code has access to pandas (pd), numpy (np), "
            "matplotlib.pyplot (plt), seaborn (sns), and insightbench.tools. "
            "Use this to create visualizations and compute statistics "
            "AFTER confirming column names via browse_semantics."
            "IMPORTANT: Save all output files (plot.jpg, stat.json, x_axis.json, "
            "y_axis.json) to the current working directory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Complete, self-contained Python script to execute. "
                        "Must include all necessary imports. "
                        "Read data from the CSV path given in the prompt. "
                        "Save output files (plot.jpg, stat.json, x_axis.json, "
                        "y_axis.json) to the current directory. "
                        "Print key results for verification."
                    ),
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

# ---------------------------------------------------------------------------

#   {"call_id": str, "name": str, "arguments": dict | str, "protocol": "native"|"dsml"}
# ---------------------------------------------------------------------------


_DSML_INVOKE_RE = re.compile(
    r'<[^<>]*DSML[^<>]*invoke'
    r'[^<>]*\bname\s*=\s*["\']([^"\']+)["\']'
    r'[^<>]*>'
    r'(.*?)'
    r'</[^<>]*DSML[^<>]*invoke\s*>',
    re.IGNORECASE | re.DOTALL,
)


_DSML_PARAM_RE = re.compile(
    r'<[^<>]*DSML[^<>]*parameter'
    r'[^<>]*\bname\s*=\s*["\']([^"\']+)["\']'
    r'[^<>]*>'
    r'(.*?)'
    r'</[^<>]*DSML[^<>]*parameter\s*>',
    re.IGNORECASE | re.DOTALL,
)


def _get_agent_api_key():
    """Return agent api key."""
    return os.getenv("AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")


def _get_agent_base_url():
    """Return agent base url."""
    return os.getenv("AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL")


def _get_verify_ssl():
    """Return verify ssl."""
    val = os.getenv("OPENAI_SSL_VERIFY", "true").lower()
    return val not in ("false", "0", "no", "off")


def _create_openai_client() -> OpenAI:
    """Create openai client."""

    verify_ssl = _get_verify_ssl()
    http_client = httpx.Client(verify=verify_ssl, trust_env=False)
    kwargs = {
        "api_key": _get_agent_api_key(),
        "timeout": float(os.getenv("OPENAI_TIMEOUT", "120")),
        "max_retries": 2,
        "http_client": http_client,
    }
    base_url = _get_agent_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


class ToolCallingChat:
    """Implementation of ToolCallingChat."""

    def __init__(
        self,
        layer: InsightSemanticLayer,
        model_name: str,
        stage: str,
        temperature: float = 0,
        max_tool_rounds: int = 5,
        client: Optional[OpenAI] = None,
        execution_tools: Optional[List[dict]] = None,
        workdir: Optional[str] = None,
    ):
        self.layer = layer
        self.model_name = model_name
        self.stage = stage
        self.temperature = temperature
        self.max_tool_rounds = max(0, int(max_tool_rounds))
        self.client = client
        self.execution_tools = list(execution_tools or [])
        self.workdir = os.path.abspath(workdir) if workdir else None

        self._verbose = os.environ.get("INSIGHTBENCH_VERBOSE", "1") == "1"

        self._execute_python_count = 0

        self._last_executed_code: Optional[str] = None
        self._last_execution_output: Optional[str] = None

    @property
    def available_tool_names(self) -> List[str]:
        """Implement available tool names."""
        names = list(self.layer.available_tool_names) if self.layer is not None else []
        for tool in self.execution_tools:
            try:
                names.append(tool["function"]["name"])
            except (KeyError, TypeError):
                pass
        return names

    def _all_tool_schemas(self) -> List[dict]:
        """Implement all tool schemas."""
        semantic_tools = self.layer.tool_schemas() if self.layer is not None else []
        return semantic_tools + self.execution_tools

    # -------------------------------------------------------------------

    # -------------------------------------------------------------------

    @staticmethod
    def _normalize_native_tool_calls(message) -> List[Dict[str, Any]]:
        """Normalize native tool calls."""
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        return [
            {
                "call_id": call.id,
                "name": call.function.name,
                "arguments": call.function.arguments,
                "protocol": "native",
            }
            for call in tool_calls
        ]

    @staticmethod
    def _parse_dsml_tool_calls(
        content: str, available_names: List[str]
    ) -> List[Dict[str, Any]]:
        """Parse dsml tool calls."""
        if not content or "DSML" not in str(content).upper():
            return []

        text = str(content)
        unified_calls: List[Dict[str, Any]] = []

        for match in _DSML_INVOKE_RE.finditer(text):
            tool_name = match.group(1)

            inner = match.group(2)
            arguments: Dict[str, Any] = {}
            for pm in _DSML_PARAM_RE.finditer(inner):
                key = pm.group(1)
                value = pm.group(2).strip()

                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    pass
                arguments[key] = value

            unified_calls.append(
                {
                    "call_id": f"dsml_{len(unified_calls)}",
                    "name": tool_name,
                    "arguments": arguments,
                    "protocol": "dsml",
                    "registered": tool_name in available_names,
                }
            )

        return unified_calls

    # -------------------------------------------------------------------

    # -------------------------------------------------------------------

    @staticmethod
    def _build_error(
        error_type: str, message: str, retryable: bool = True
    ) -> str:
        """Build error."""
        return json.dumps(
            {
                "status": "error",
                "error_type": error_type,
                "message": message,
                "retryable": retryable,
            },
            ensure_ascii=False,
        )

    # -------------------------------------------------------------------

    # -------------------------------------------------------------------

    def _handle_execute_python(self, call: dict) -> str:
        """Implement handle execute python."""
        self._execute_python_count += 1
        count = self._execute_python_count


        if count >= 5:
            if self._verbose:
                print(
                    f"  [{self.stage}] !! execute_python HARD BLOCK (call #{count})",
                    flush=True,
                )
            return json.dumps(
                {
                    "status": "blocked",
                    "message": (
                        "execute_python call limit (5) reached. "
                        "Return your best answer based on available information. "
                        "Do NOT call execute_python again."
                    ),
                },
                ensure_ascii=False,
            )


        args = call.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return self._build_error(
                    "invalid_arguments",
                    "execute_python requires valid JSON arguments with a 'code' field.",
                )

        code = str(args.get("code", "") or "")
        if not code.strip():
            return self._build_error(
                "invalid_arguments",
                "execute_python requires non-empty 'code' parameter.",
            )


        from insightbench.utils.agent_utils import PythonREPL

        output, valid, error = PythonREPL().run(code, workdir=self.workdir)

        if valid:
            self._last_executed_code = code
            self._last_execution_output = output
            result: Dict[str, Any] = {"status": "ok", "stdout": output}
        else:

            safe_error = error.encode("ascii", errors="replace").decode("ascii")
            result = {"status": "error", "stdout": output, "stderr": safe_error}


        if count == 3:
            result["warning"] = (
                "execute_python has been called 3 times. "
                "Consider using browse_semantics to verify "
                "column names, schema, or data types before your next code attempt. "
                f"You have {5 - count} execute_python calls remaining before hard block."
            )

        return json.dumps(result, ensure_ascii=False)

    # -------------------------------------------------------------------

    # -------------------------------------------------------------------

    def __call__(self, content: str) -> str:
        client = self.client or _create_openai_client()
        messages: List[Dict[str, Any]] = [{"role": "user", "content": content}]
        available_names = self.available_tool_names



        for round_idx in range(self.max_tool_rounds + 1):
            allow_tools = self.max_tool_rounds > 0
            rounds_left = self.max_tool_rounds - round_idx
            kwargs: Dict[str, Any] = {
                "model": self.model_name,
                "temperature": self.temperature,
                "messages": messages,
            }
            if allow_tools:
                kwargs.update(
                    {"tools": self._all_tool_schemas(), "tool_choice": "auto"}
                )


            if rounds_left == 2 and allow_tools:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You have only 2 tool-calling rounds remaining. "
                            "After your next execute_python call, you MUST return "
                            "a plain-text summary confirming the generated artifacts "
                            "(plot.jpg, stat.json, x_axis.json, y_axis.json). "
                            "Do NOT call further tools after that."
                        ),
                    }
                )


            tool_names_hint = ", ".join(self.available_tool_names)
            if self._verbose:
                print(
                    f"  [{self.stage}] round {round_idx + 1}/{self.max_tool_rounds + 1} "
                    f"(tools: {tool_names_hint}) — calling LLM...",
                    flush=True,
                )

            response = client.chat.completions.create(**kwargs)
            message = response.choices[0].message


            native_calls = self._normalize_native_tool_calls(message)


            dsml_calls: List[Dict[str, Any]] = []
            if not native_calls and message.content:
                dsml_calls = self._parse_dsml_tool_calls(
                    message.content, available_names
                )



            has_dsml = bool(
                message.content and "DSML" in str(message.content).upper()
            )
            if not native_calls and has_dsml and not dsml_calls:
                if self._verbose:
                    print(
                        f"  [{self.stage}] DSML parse error, retrying...",
                        flush=True,
                    )
                error = self._build_error(
                    "protocol_error",
                    "The DSML tool call is incomplete or malformed. Retry with "
                    "a complete registered semantic tool call, or return the "
                    "final answer without DSML markup.",
                )
                messages.append(
                    {"role": "assistant", "content": message.content or ""}
                )
                messages.append({"role": "user", "content": error})
                continue

            all_calls = native_calls + dsml_calls


            if not all_calls:
                if self._execute_python_count == 0:
                    if self._verbose:
                        print(
                            f"  [{self.stage}] !! LLM tried to finish without execute_python — "
                            f"injecting reminder",
                            flush=True,
                        )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You have not called execute_python yet. You MUST "
                                "call execute_python at least once to generate and "
                                "run analysis code that produces the required "
                                "artifacts (plot.jpg, stat.json, x_axis.json, "
                                "y_axis.json). Do not end the conversation without "
                                "running code."
                            ),
                        }
                    )
                    continue
                if self._verbose:
                    print(
                        f"  [{self.stage}] done (no tool calls — final answer)",
                        flush=True,
                    )
                return message.content or ""




            tool_results: List[tuple] = []
            for call in all_calls:
                call_name = call["name"]
                if call_name == "execute_python":
                    if self._verbose:
                        print(
                            f"  [{self.stage}] → execute_python "
                            f"(#{self._execute_python_count + 1}/5)",
                            flush=True,
                        )
                else:
                    if self._verbose:
                        print(
                            f"  [{self.stage}] → {call_name}",
                            flush=True,
                        )
                try:
                    if call_name == "execute_python":
                        result = self._handle_execute_python(call)
                    elif call_name not in available_names:
                        result = self._build_error(
                            "unknown_tool",
                            f"Unknown tool: {call['name']}. Available tools: "
                            + ", ".join(available_names),
                        )
                    elif call["protocol"] == "native":
                        args_json = call["arguments"]
                        if not isinstance(args_json, str):
                            args_json = json.dumps(args_json, ensure_ascii=False)
                        result = self.layer.execute_json(
                            call["name"], args_json, self.stage
                        )
                    else:
                        args_json = json.dumps(
                            call["arguments"], ensure_ascii=False
                        )
                        result = self.layer.execute_json(
                            call["name"], args_json, self.stage
                        )
                except json.JSONDecodeError as exc:
                    result = self._build_error(
                        "invalid_arguments",
                        f"Invalid JSON arguments for {call['name']}: {exc}",
                    )
                except (KeyError, TypeError) as exc:
                    result = self._build_error(
                        "invalid_arguments",
                        f"Invalid arguments for {call['name']}: {exc}",
                    )
                except Exception as exc:
                    result = self._build_error(
                        "internal_error", str(exc)
                    )
                tool_results.append((call, result))

                if self._verbose:
                    try:
                        r = json.loads(result)
                        status = r.get("status", "?")
                        print(f"    → {status}", flush=True)
                    except Exception:
                        print("    → done", flush=True)


            if native_calls:

                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [
                            {
                                "id": call["call_id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": (
                                        call["arguments"]
                                        if call["protocol"] == "native"
                                        else json.dumps(
                                            call["arguments"],
                                            ensure_ascii=False,
                                        )
                                    ),
                                },
                            }
                            for call in native_calls
                        ],
                    }
                )
                for call, result in tool_results:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["call_id"],
                            "content": result,
                        }
                    )
            else:



                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                    }
                )
                feedback = {
                    "type": "tool_results",
                    "results": [
                        {
                            "call_id": call["call_id"],
                            "name": call["name"],
                            "result": json.loads(result),
                        }
                        for call, result in tool_results
                    ],
                }
                messages.append(
                    {
                        "role": "user",
                        "content": json.dumps(feedback, ensure_ascii=False),
                    }
                )

        if self._verbose:
            print(
                f"  [{self.stage}] EXHAUSTED {self.max_tool_rounds + 1} rounds — "
                f"RuntimeError",
                flush=True,
            )
        raise RuntimeError(
            "ToolCallingChat exceeded its safety turn limit before the Agent "
            "returned a final answer."
        )
