"""
Optional LLM assistance for the escalation queue.

Design intent: the core pipeline (ingest/map/clean/reconcile/validate) stays
fully deterministic and rule-based -- that's what makes the autonomy
boundary in agent.py explainable and testable. The LLM is used ONLY at the
edges, to help a human resolve an escalation faster: given the exact same
context a human reviewer sees, gpt-4o-mini proposes a value and a one-line
rationale. It is never auto-applied -- it shows up in the escalation card as
a suggestion with a confidence level, and the human still clicks
approve/correct/reject. If no API key is configured, or the call fails or
times out, this degrades silently to "no suggestion" and the rest of the
app behaves exactly as it does without an LLM in the loop.
"""

from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
_ENABLED = bool(os.getenv("OPENAI_API_KEY"))
_client = None


def enabled() -> bool:
    return _ENABLED


def _get_client():
    global _client
    if not _ENABLED:
        return None
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client


def _build_prompt(kind: str, title: str, detail: str, context: dict[str, Any], options: list[str]) -> str:
    is_name_split = context.get("field") == "__full_name__"
    shape_instruction = (
        '"suggestion" must be an object: {"first_name": "...", "last_name": "..."}'
        if is_name_split else
        '"suggestion" must be a single string -- ideally one of the listed options, '
        "or a corrected value if none of the options are appropriate"
    )
    return f"""A data migration agent hit a case it isn't confident enough to resolve on its own, \
and is asking a human implementation consultant to decide. Propose the most likely correct \
resolution to help the human resolve it in one glance. Be conservative: if you are genuinely \
unsure, say so with low confidence rather than guessing.

Escalation type: {kind}
Title: {title}
Why it was escalated: {detail}
Context: {json.dumps(context, default=str)}
Candidate options already identified: {options}

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"suggestion": <see shape below>, "rationale": "<one sentence, plain English>", "confidence": "low|medium|high"}}
{shape_instruction}
"""


def suggest_escalation_resolution(kind: str, title: str, detail: str,
                                   context: dict[str, Any], options: list[str]) -> dict | None:
    """Returns {"suggestion", "rationale", "confidence", "model"} or None if
    the LLM is not configured. Never raises -- a failed call just yields a
    dict with an "error" key so the UI can show 'AI suggestion unavailable'
    instead of breaking the escalation card."""
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You output only valid JSON, nothing else."},
                {"role": "user", "content": _build_prompt(kind, title, detail, context, options)},
            ],
            temperature=0,
            max_tokens=250,
            timeout=8,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return {
            "suggestion": data.get("suggestion"),
            "rationale": data.get("rationale", ""),
            "confidence": data.get("confidence", "low"),
            "model": MODEL,
        }
    except Exception as e:
        return {"error": str(e), "model": MODEL}
