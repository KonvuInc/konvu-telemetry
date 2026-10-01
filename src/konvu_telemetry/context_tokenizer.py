"""Estimate provider input tokens without retaining source content."""

from __future__ import annotations

import importlib
import json
import math
import re
from types import ModuleType


def _load_optional(name: str) -> ModuleType | None:
    try:
        return importlib.import_module(name)
    except (ImportError, OSError):
        return None


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
    if re.search(r"claude-(?:opus|sonnet|fable)-5(?:-|$)", normalized):
        return "5.0"
    if re.search(r"claude-(?:opus|sonnet|fable)-4[.-]8(?:-|$)", normalized):
        return "4.8"
    if re.search(r"claude-(?:opus|sonnet|fable)-4[.-]7(?:-|$)", normalized):
        return "4.7"
    return "3.0"


class ContextTokenizer:
    """Use provider tokenizers when installed and a stable fallback otherwise."""

    def __init__(self) -> None:
        self._tiktoken = _load_optional("tiktoken")
        self._ctok = _load_optional("ctok")
        self._codex_encoding: object | None = None

    def count(self, provider: str, model: str, value: object) -> tuple[int, str]:
        content = _text(value)
        if not content:
            return 0, "empty"
        if provider == "codex" and self._tiktoken is not None:
            try:
                if self._codex_encoding is None:
                    self._codex_encoding = self._tiktoken.get_encoding("o200k_base")
                encode = getattr(self._codex_encoding, "encode")
                return len(encode(content, disallowed_special=())), "o200k_base"
            except Exception:
                pass
        if provider == "claude" and self._ctok is not None:
            try:
                version = claude_tokenizer_version(model)
                return int(
                    self._ctok.token_count(content, version=version)
                ), f"ctok-{version}"
            except Exception:
                pass
        byte_count = len(content.encode("utf-8"))
        return max(1, math.ceil(byte_count / 4)), "utf8-4-byte-fallback"
