"""
LLM integration module for AI-assisted escalation resolution suggestions.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if GROQ_API_KEY:
    PROVIDER = "Groq"
    MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    _ENABLED = True
elif OPENAI_API_KEY:
    PROVIDER = "OpenAI"
    MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    _ENABLED = True
else:
    PROVIDER = None
    MODEL = os.getenv("GROQ_MODEL", os.getenv("OPENAI_MODEL", "llama-3.3-70b-versatile"))
    _ENABLED = False

_client = None


def enabled() -> bool:
    return _ENABLED


def _get_client():
    global _client
    if not _ENABLED:
        return None
    if _client is None:
        from openai import OpenAI
        if GROQ_API_KEY:
            _client = OpenAI(
                api_key=GROQ_API_KEY,
                base_url="https://api.groq.com/openai/v1"
            )
        elif OPENAI_API_KEY:
            _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _build_prompt(kind: str, title: str, detail: str, context: dict[str, Any], options: list[str]) -> str:
    is_name_split = context.get("field") == "__full_name__"
    shape_instruction = (
        '"suggestion" must be an object: {"first_name": "...", "last_name": "..."}'
        if is_name_split else
        '"suggestion" must be a single string from candidate options or a corrected value'
    )
    return f"""Analyze the following data escalation case from an automated migration pipeline:

Escalation Type: {kind}
Title: {title}
Reason: {detail}
Context: {json.dumps(context, default=str)}
Options: {options}

Recommend the most likely resolution.
Respond with ONLY valid JSON with keys "suggestion", "rationale", and "confidence" ("low", "medium", "high").
{shape_instruction}
"""


def suggest_escalation_resolution(kind: str, title: str, detail: str,
                                   context: dict[str, Any], options: list[str]) -> Optional[dict]:
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a data migration assistant. Output valid JSON only."},
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
