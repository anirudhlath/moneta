"""Conservative provider account identity hints; hints alone never merge accounts."""

import re


def infer_mask(name: str) -> str | None:
    """Read an explicit terminal four-digit parenthesized account suffix."""
    match = re.search(r"\(([0-9]{4})\)\s*$", name)
    return match.group(1) if match else None
