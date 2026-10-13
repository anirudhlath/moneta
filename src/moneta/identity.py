"""Conservative provider account identity hints; hints alone never merge accounts."""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from moneta.models import Transaction


def infer_mask(name: str) -> str | None:
    """Read an explicit terminal four-digit parenthesized account suffix."""
    match = re.search(r"\(([0-9]{4})\)\s*$", name)
    return match.group(1) if match else None


def normalize_institution(name: str) -> str:
    """Normalize presentation and exact bank aliases, never fuzzy substrings."""
    normalized = " ".join(name.casefold().split())
    aliases = {
        "chase bank": "chase",
        "jpmorgan chase": "chase",
        "jpmorgan chase bank": "chase",
        "jpmorgan chase bank, n.a.": "chase",
        "amex": "american express",
    }
    return aliases.get(normalized, normalized)


def compatible_descriptors(left: "Transaction", right: "Transaction") -> bool:
    """Exact normalized identity only; shared amounts or generic substrings are not identity.

    Raw descriptors remain useful when a reviewed merchant renamed just one source.
    """
    from moneta.pipelines.normalize import rule_normalize

    def normalized(value: str) -> str:
        return " ".join(re.findall(r"\w+", rule_normalize(value).casefold()))

    raw_left, raw_right = normalized(left.description), normalized(right.description)
    if raw_left and raw_left == raw_right:
        return True
    return bool(
        left.merchant
        and right.merchant
        and normalized(left.merchant)
        not in {
            "payment",
            "transfer",
            "deposit",
            "withdrawal",
            "purchase",
            "ach",
            "debit",
            "credit",
            "unknown",
        }
        and normalized(left.merchant) == normalized(right.merchant)
    )
