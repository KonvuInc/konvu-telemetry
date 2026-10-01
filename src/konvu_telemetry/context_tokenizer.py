"""Estimate provider input tokens without retaining source content."""

from __future__ import annotations

import ctok  # type: ignore[import-untyped]
import json
import re
import tiktoken


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                content = item.get("text") if kind in {"text", "input_text"} else None
                if isinstance(content, str):
                    parts.append(content)
                else:
                    parts.append(
                        json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    )
        if parts:
            return "\n".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def claude_tokenizer_version(model: str) -> str:
    """Choose the tokenizer family for the model consuming this addition."""
    normalized = model.lower()
    version = re.search(
        r"claude-[a-z0-9]+-(\d)(?:(?:[.-](\d{1,2}))(?=-|$)|(?=-|$))",
        normalized,
    )
    if version is None:
        return "3.0"
    major = int(version.group(1))
    minor = int(version.group(2) or 0)
    if major >= 5:
        return "5.0"
    if major == 4 and minor >= 8:
        return "4.8"
    if major == 4 and minor >= 7:
        return "4.7"
    return "3.0"


class ContextTokenizer:
    """Count context text with the pinned provider-family tokenizers."""

    def __init__(self) -> None:
        self._tiktoken = tiktoken
        self._ctok = ctok
        self._codex_encoding: tiktoken.Encoding | None = None

    def count(self, provider: str, model: str, value: object) -> tuple[int, str]:
        content = _text(value)
        if not content:
            return 0, "empty"
        if provider == "codex":
            if self._codex_encoding is None:
                self._codex_encoding = self._tiktoken.get_encoding("o200k_base")
            return (
                len(self._codex_encoding.encode(content, disallowed_special=())),
                "o200k_base",
            )
        if provider == "claude":
            version = claude_tokenizer_version(model)
            return int(
                self._ctok.token_count(content, version=version)
            ), f"ctok-{version}"
        raise ValueError(f"Unsupported context provider: {provider}")
