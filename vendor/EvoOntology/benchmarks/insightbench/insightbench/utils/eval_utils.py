from insightbench import prompts

import numpy as np, pandas as pd, time, re, os
import evaluate

import httpx
import requests

import openai
from openai import OpenAI

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")


def _get_verify_ssl():
    """Return verify ssl."""
    val = os.getenv("OPENAI_SSL_VERIFY", "true").lower()
    return val not in ("false", "0", "no", "off")


def _get_eval_api_key():
    """Return eval api key."""
    return os.getenv("EVAL_API_KEY") or os.getenv("AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")


def _get_eval_base_url():
    """Return eval base url."""
    return os.getenv("EVAL_BASE_URL") or os.getenv("AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL")


def _create_openai_client():
    """Create openai client."""

    verify_ssl = _get_verify_ssl()
    http_client = httpx.Client(verify=verify_ssl, trust_env=False)
    kwargs = {
        "api_key": _get_eval_api_key(),
        "timeout": float(os.getenv("OPENAI_TIMEOUT", "300")),
        "max_retries": 2,
        "http_client": http_client,
    }
    base_url = _get_eval_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _is_openai_api():
    """Implement is openai api."""
    base_url = _get_eval_base_url()
    return not base_url


def _get_eval_model():
    """Return eval model."""
    return os.getenv("EVAL_MODEL_NAME", "gpt-4o")


def _parse_rating(content) -> float:
    """Parse a 0-10 rating from plain text or ``<rating>`` output."""
    text = str(content).strip()
    patterns = [
        r"<rating>\s*([0-9]+(?:\.[0-9]+)?)\s*</rating>",
        r"^\s*([0-9]+(?:\.[0-9]+)?)\s*$",
        r"\brating\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            rating = float(match.group(1))
            if 0 <= rating <= 10:
                return rating
            raise ValueError(f"G-Eval rating out of range: {rating}")
    raise ValueError(f"Could not parse G-Eval rating from: {text[:200]!r}")


def _eval_retry_settings():
    retries = int(os.getenv("G_EVAL_MAX_RETRIES", "5"))
    base_delay = float(os.getenv("G_EVAL_RETRY_BASE_SECONDS", "2"))
    return max(retries, 0), max(base_delay, 0.0)


def compute_g_eval(answer, gt_answer, model_name=None, top_logprobs=None):

    if not _is_openai_api():
        top_logprobs = None
    model_name = model_name or _get_eval_model()
    client = _create_openai_client()
    return compute_llm_eval(client, answer, gt_answer, model_name, top_logprobs)


def is_llama_running(model_name):
    status_code = requests.post(
        "http://0.0.0.0:8085/v1/completions",
        json={"prompt": "Hello!", "model": model_name},
        headers={
            "Content-Type": "application/json",
            "Cookie": "sessiona=1687876608.234.49.972136|78cabb3f310793e5a58a141fe9058709",
            "Authorization": "EMPTY",
        },
    ).status_code
    return status_code == 200


def compute_llama3_eval(
    answer, gt_answer, model_name="meta-llama/Meta-Llama-3-70B", top_logprobs=None
):
    """Compute LLaMA-3-Eval score between answer and gt_answer"""
    # check if llama3 is running locally
    if is_llama_running(model_name):
        client = OpenAI(api_key="EMPTY", base_url="http://0.0.0.0:8085/v1/")
        return compute_llm_eval(client, answer, gt_answer, model_name, top_logprobs)
    else:
        raise RuntimeError(
            """
To use LLaMA-3-Eval, please first host a LLaMA-3 model locally using the vllm library:
```
pip install vllm
python -u -m vllm.entrypoints.openai.api_server --host 0.0.0.0 --model meta-llama/Meta-Llama-3-70B --tensor-parallel-size 8 --load-format safetensors --port 8085 --dtype half --gpu-memory-utilization 0.8 --max-model-len 8000 --enforce-eager
```
"""
        )


def compute_llm_eval(
    client,
    answer,
    gt_answer,
    model_name="gpt-4o",
    top_logprobs=None,
    max_retries=None,
):
    template, system_message = prompts.get_g_eval_prompt(method="basic")

    prompt = template.format(answer=answer, gt_answer=gt_answer)
    configured_retries, base_delay = _eval_retry_settings()
    max_retries = configured_retries if max_retries is None else max(max_retries, 0)
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            kwargs = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 4096,
                "top_p": 1,
            }

            if top_logprobs:
                kwargs["logprobs"] = True
                kwargs["top_logprobs"] = top_logprobs
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content
            if not top_logprobs:
                return _parse_rating(content)
            else:
                # get the index in response where we have the rating
                direct_rating = _parse_rating(content)
                rating_str = str(int(direct_rating)) if direct_rating.is_integer() else str(direct_rating)
                tokens = [o.token for o in response.choices[0].logprobs.content]
                rating_idx_in_response = next(
                    (i for i, token in enumerate(tokens) if token.strip() == rating_str),
                    None,
                )
                if rating_idx_in_response is None:
                    return direct_rating
                response = (
                    response.choices[0]
                    .logprobs.content[rating_idx_in_response]
                    .top_logprobs
                )
                # convert logprobs to probs
                probs = [np.exp(obj.logprob) for obj in response]
                # renormalize probs to sum to 1
                probs = [obj / sum(probs) for obj in probs]
                ratings = [
                    float(obj.token.strip())
                    if obj.token.strip().replace(".", "", 1).isdigit()
                    else 0
                    for obj in response
                ]
                # final score
                return sum([a * b for a, b in zip(ratings, probs)])
        except Exception as e:
            last_error = e
            if attempt >= max_retries:
                break
            delay = min(base_delay * (2 ** attempt), 30.0)
            print(
                f"G-Eval attempt {attempt + 1}/{max_retries + 1} failed: "
                f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s..."
            )
            if delay:
                time.sleep(delay)

    raise RuntimeError(
        f"G-Eval failed after {max_retries + 1} attempts: {last_error}"
    ) from last_error


def _parse_m2m_response(content: str, n_pred: int) -> list:
    """Parse matched prediction indices from LLM g_eval_m2m response.

    Two-pass strategy:
    1. Strict: one integer (or -1) per line (the expected format)
    2. Fallback: per-line — if a line contains exactly one integer and it is a
       valid match index (-1 or 1..n_pred), use it

    Returns a list of matched indices (1-indexed, -1 = no match).
    """
    content = str(content or "")

    # ── Pass 1: strict line-by-line ──────────────────────────
    matched: list[int] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        val = None
        # Pure integer (including negative): "3" or "-1"
        if stripped.isdigit() or (stripped.startswith('-') and stripped[1:].isdigit()):
            val = int(stripped)
        else:
            # Numbered-prefix format: "1. 3", "1: 3", "3. -1"
            parsed = re.sub(r"\d+\s*[.:]\s*(-?\d+).*", r"\1", stripped).strip()
            if re.fullmatch(r"-?\d+", parsed):
                val = int(parsed)
        # Accept only valid match indices
        if val is not None and (val == -1 or (1 <= val <= n_pred)):
            matched.append(val)

    # ── Pass 2 (fallback): per-line exactly-one-integer ───────
    if not matched:
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            ints = re.findall(r'-?\d+', stripped)
            if len(ints) == 1:
                val = int(ints[0])
                if val == -1 or (1 <= val <= n_pred):
                    matched.append(val)

    return matched


def compute_g_eval_m2m(
    pred_insights, gt_insights, model_name=None, top_logprobs=None
):
    """Does many-to-many matching of provided and gt insights"""

    if not _is_openai_api():
        top_logprobs = None
    model_name = model_name or _get_eval_model()
    client = _create_openai_client()
    template = prompts.G_EVAL_M2M_TEMPLATE
    pred_insights_formatted = "\n".join(
        [f"{idx+1}. {a}" for idx, a in enumerate(pred_insights)]
    )
    gt_answers_formatted = "\n".join(
        [f"{idx+1}. {a}" for idx, a in enumerate(gt_insights)]
    )
    prompt = template.format(
        pred_list=pred_insights_formatted, gt_list=gt_answers_formatted,
        n_gt=len(gt_insights),
    )
    if not pred_insights:
        return 0.0, []

    max_retries, base_delay = _eval_retry_settings()
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            kwargs = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": prompts.G_EVAL_M2M_SYSTEM_MESSAGE},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 4096,
                "top_p": 1,
            }
            if top_logprobs:
                kwargs["logprobs"] = True
                kwargs["top_logprobs"] = top_logprobs
            response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content

            # ── Parse matched indices from LLM response ──────────
            matched_responses = _parse_m2m_response(content, len(pred_insights))

            if len(matched_responses) < len(gt_insights):
                raise ValueError(
                    "G-Eval matcher returned fewer matches than ground truths: "
                    f"{len(matched_responses)} < {len(gt_insights)}"
                )
            scores_dict = []
            for id, mid in enumerate(matched_responses[:len(gt_insights)]):
                mid = mid - 1 if mid > 0 else np.random.choice(len(pred_insights))
                if mid >= len(pred_insights):
                    raise ValueError(f"G-Eval matcher returned invalid prediction index: {mid + 1}")
                score = (
                    compute_g_eval(
                        pred_insights[mid],
                        gt_insights[id],
                        model_name,
                        top_logprobs,
                    )
                    / 10.0
                )
                scores_dict.append(
                    {
                        "pred_insight": pred_insights[mid],
                        "gt_insight": gt_insights[id],
                        "score": score,
                    }
                )
            score = np.mean([score["score"] for score in scores_dict])
            return score, scores_dict
        except Exception as e:
            last_error = e
            if attempt >= max_retries:
                break
            delay = min(base_delay * (2 ** attempt), 30.0)
            print(
                f"G-Eval matching attempt {attempt + 1}/{max_retries + 1} failed: "
                f"{type(e).__name__}: {e}. Retrying in {delay:.1f}s..."
            )
            if delay:
                time.sleep(delay)

    raise RuntimeError(
        f"G-Eval matching failed after {max_retries + 1} attempts: {last_error}"
    ) from last_error


_ROUGE_SCORER = None


def _get_rouge_scorer():
    """Lazy-load the ROUGE scorer once (module-level cache)."""
    global _ROUGE_SCORER
    if _ROUGE_SCORER is None:
        _ROUGE_SCORER = evaluate.load("rouge")
    return _ROUGE_SCORER


def compute_rouge_score(answer, gt_answer, **kwargs):
    """Compute ROUGE-1 between answer and gt_answer"""
    return _get_rouge_scorer().compute(
        predictions=[answer],
        references=[gt_answer],
        rouge_types=["rouge1"],
    )["rouge1"]
