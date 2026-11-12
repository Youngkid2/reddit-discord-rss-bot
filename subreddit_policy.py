"""Subreddit name policy: parsing, autocomplete helpers, and the blocklist.

Only minors-related and rape/non-consent names are blocked. Everything else
is allowed, including bondage, hentai, gore, and incest.
"""

from __future__ import annotations

import re


MINOR_TERMS = (
    "teen",
    "young",
    "loli",
    "shota",
    "jailbait",
    "underage",
    "child",
    "kid",
    "minor",
    "schoolgirl",
)

NONCONSENT_TERMS = (
    "rape",
    "noncon",
    "non_con",
    "nonconsent",
    "cnc",
    "forced",
    "drugged",
    "sleeping",
    "unaware",
)

BLOCKED_TERMS = MINOR_TERMS + NONCONSENT_TERMS

SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,50}$")


def normalize_subreddit_name(name: str | None) -> str:
    if not name:
        return ""
    value = str(name).strip()
    value = value.lstrip("/").strip()
    if value.lower().startswith("r/"):
        value = value[2:].strip()
    value = value.strip().strip("/")
    if value.startswith("/"):
        value = value[1:]
    return value.strip()


def parse_subreddit_list(text: str | None) -> list[str]:
    """Split a comma-separated subreddit list, dropping blanks and duplicates."""
    if not text:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for part in str(text).split(","):
        name = normalize_subreddit_name(part)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def is_valid_subreddit_name(name: str | None) -> bool:
    normalized = normalize_subreddit_name(name)
    return bool(SUBREDDIT_NAME_RE.fullmatch(normalized))


def _normalized_name_forms(name: str) -> tuple[str, str]:
    raw = normalize_subreddit_name(name).lower().replace("-", "_")
    compact = raw.replace("_", "")
    return raw, compact


def blocked_reason(name: str | None) -> str | None:
    """Return a short reason if this subreddit name is blocked, else None."""
    raw, compact = _normalized_name_forms(name or "")
    if not raw:
        return None

    for term in MINOR_TERMS:
        if _term_matches(raw, compact, term):
            return "minors-related"
    for term in NONCONSENT_TERMS:
        if _term_matches(raw, compact, term):
            return "rape/non-consent"
    return None


def _term_matches(raw: str, compact: str, term: str) -> bool:
    token = term.lower().replace("-", "_")
    compact_term = token.replace("_", "")
    if not compact_term:
        return False
    return token in raw or compact_term in compact


def is_blocked_subreddit(name: str | None) -> bool:
    return blocked_reason(name) is not None


def allowed_subreddit_names(names: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Split names into (allowed, [(blocked_name, reason), ...])."""
    allowed: list[str] = []
    blocked: list[tuple[str, str]] = []
    for name in names:
        reason = blocked_reason(name)
        if reason:
            blocked.append((name, reason))
        else:
            allowed.append(name)
    return allowed, blocked


def split_csv_autocomplete(current: str | None) -> tuple[str, str]:
    """
    Split a slash-command autocomplete string so only the last
    comma-separated item is used as the search query.

    "aww, bond" -> ("aww, ", "bond")
    "bondage"   -> ("", "bondage")
    "aww,"      -> ("aww, ", "")
    """
    text = "" if current is None else str(current)
    if "," not in text:
        return "", text.strip()

    index = text.rfind(",")
    after = text[index + 1 :]
    spacing = after[: len(after) - len(after.lstrip())]
    if not spacing:
        spacing = " "
    prefix = text[: index + 1] + spacing
    return prefix, after.strip()


def format_subscriber_count(count) -> str | None:
    """Format 733000 as '733K', 1200000 as '1.2M'."""
    try:
        number = int(count)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if number >= 1_000_000:
        value = number / 1_000_000
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}M"
    if number >= 1_000:
        value = number / 1_000
        if value >= 10:
            return f"{int(round(value))}K"
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}K"
    return str(number)


def format_subreddit_choice(name: str, subscribers=None) -> str:
    count = format_subscriber_count(subscribers)
    if count:
        return f"{name} ({count})"
    return name


def blocked_refusal_message(blocked: list[tuple[str, str]]) -> str:
    if not blocked:
        return ""
    parts = [f"r/{name} ({reason})" for name, reason in blocked]
    return (
        "I can't add "
        + ", ".join(parts)
        + ". That subreddit is blocked because it is minors-related or "
        "rape/non-consent."
    )
