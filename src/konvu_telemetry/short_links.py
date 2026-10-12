"""Short local dashboard links that stand in for long per-session URLs."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

from .config import ALLOWED_PROVIDERS
from .storage import home_dir, valid_session_id

CODE_LENGTH = 7
ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
# Short-link path prefix to the dashboard tab it opens.
TABS = {"s": None, "c": "context"}


def short_code(provider: str, session_id: str) -> str:
    """A stable seven-character code for one session, derived from its identity."""
    value = int.from_bytes(
        hashlib.sha256(f"{provider}:{session_id}".encode()).digest()[:8], "big"
    )
    code = ""
    for _ in range(CODE_LENGTH):
        value, digit = divmod(value, len(ALPHABET))
        code += ALPHABET[digit]
    return code


def short_link_path(provider: str, session_id: str, tab: str | None = None) -> str:
    """The dashboard path that redirects to this session, opened on one tab."""
    prefix = next(key for key, value in TABS.items() if value == tab)
    return f"/{prefix}/{short_code(provider, session_id)}"


def resolve_short_code(code: str) -> tuple[str, str] | None:
    """Find the recorded session a code names; an unknown or ambiguous code is None.

    Codes are not stored anywhere: the collector's per-session files are the registry.
    """
    if len(code) != CODE_LENGTH or not all(char in ALPHABET for char in code):
        return None
    matches = []
    for path in (home_dir() / "sessions").glob("*.json"):
        provider, _, session_id = path.stem.partition("-")
        if (
            provider in ALLOWED_PROVIDERS
            and valid_session_id(session_id)
            and short_code(provider, session_id) == code
        ):
            matches.append((provider, session_id))
    return matches[0] if len(matches) == 1 else None


def resolved_dashboard_path(link_path: str) -> str | None:
    """Map a short-link path like /c/abc1234 to its full dashboard path, or None."""
    prefix, _, code = link_path.strip("/").partition("/")
    if prefix not in TABS:
        return None
    session = resolve_short_code(code)
    if session is None:
        return "/"
    tab = TABS[prefix]
    return (
        "/?session="
        + quote(":".join(session), safe="")
        + (f"&tab={tab}" if tab else "")
    )
