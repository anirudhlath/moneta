"""LLM boundary. Classification only — never arithmetic, never the money path."""

import json
from typing import Any, Protocol

from loguru import logger


# Some providers (notably OpenRouter routes) ignore response_format and wrap the
# JSON in markdown fences and/or explanatory prose before or after it.
def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in LLM response")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("LLM response is not a JSON object")
    return obj


class Classifier(Protocol):
    async def classify_json(self, prompt: str) -> dict[str, Any] | None: ...


# Shared bill/habit/not_recurring definitions for every recurring-classification prompt
# (initial detection, LLM verification, auto-review) — one place to keep the taxonomy
# consistent.
CLASSIFICATION_TAXONOMY = (
    '- "bill": a fixed obligation — subscription, rent, insurance, loan or membership payment; '
    "roughly stable amount; there are consequences if unpaid. For money coming in, a regular "
    'income stream — a paycheck, a benefit, a recurring transfer in — is also a "bill".\n'
    '- "habit": recurring discretionary spending — restaurants, coffee, bars, groceries, '
    "rideshare; variable amounts; a fresh choice each time. Spending only, never income.\n"
    '- "not_recurring": neither — coincidental repetition.'
)


def confident_yes(answer: dict[str, Any] | None, key: str) -> bool:
    """True only when the classifier answered {key: true, confident: true}."""
    return answer is not None and answer.get(key) is True and answer.get("confident") is True


class LiteLLMClassifier:
    def __init__(self, model: str, api_base: str | None = None) -> None:
        self.model = model
        self.api_base = api_base

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        import litellm

        # Self-hosted OpenAI-compatible servers (e.g. vLLM) need an explicit endpoint;
        # hosted providers resolve their own, so omit the key rather than send None.
        endpoint = {"api_base": self.api_base} if self.api_base else {}
        try:
            resp = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                **endpoint,
            )
            content = resp.choices[0].message.content
            return _extract_json_object(content)
        except Exception as exc:  # noqa: BLE001 — degrade to review queue, never crash sync
            logger.warning("LLM classification failed: {}", exc)
            return None


def build_classifier(llm_model: str | None, api_base: str | None = None) -> Classifier | None:
    return LiteLLMClassifier(llm_model, api_base) if llm_model else None
