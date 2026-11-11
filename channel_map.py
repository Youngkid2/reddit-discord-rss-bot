"""Map Discord channel names to Reddit subreddit names."""

from typing import Dict, Iterable, List, Optional, Set


DEFAULT_IGNORE_CHANNELS = ("general", "rules", "announcements")


def parse_csv_names(raw: Optional[str], defaults: Iterable[str] = ()) -> Set[str]:
    if raw is None:
        values = list(defaults)
    else:
        values = [part.strip() for part in raw.split(",")]
    return {name.lower() for name in values if name}


def parse_override_map(raw: Optional[str]) -> Dict[str, str]:
    """
    Parse CHANNEL_SUBREDDIT_MAP env values like:
    my-channel:ActualSub,foo:bar
    """
    mapping: Dict[str, str] = {}
    if not raw:
        return mapping

    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        channel_name, subreddit_name = part.split(":", 1)
        channel_name = channel_name.strip().lower()
        subreddit_name = subreddit_name.strip().lstrip("r/").strip()
        if channel_name and subreddit_name:
            mapping[channel_name] = subreddit_name

    return mapping


def is_ignored_channel(
    channel_name: str,
    ignore_names: Set[str],
    overrides: Dict[str, str],
) -> bool:
    key = channel_name.strip().lower()
    if key in overrides:
        return False
    return key in ignore_names


def subreddit_candidates(
    channel_name: str,
    overrides: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    Return subreddit names to try for a channel.

    Discord lowercases channel names and often uses hyphens, while
    subreddit names can use underscores. Try the channel name as-is,
    then with '-' replaced by '_'. An override map wins if present.
    """
    overrides = overrides or {}
    key = channel_name.strip().lower()

    if key in overrides:
        return [overrides[key]]

    raw_name = channel_name.strip()
    candidates: List[str] = []
    for name in (raw_name, raw_name.replace("-", "_")):
        if name and name.lower() not in {item.lower() for item in candidates}:
            candidates.append(name)
    return candidates
