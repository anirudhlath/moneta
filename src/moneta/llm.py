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


def confident_yes(answer: dict[str, Any] | None, key: str) -> bool:
    """True only when the classifier answered {key: true, confident: true}."""
    return answer is not None and answer.get(key) is True and answer.get("confident") is True


class LiteLLMClassifier:
    def __init__(self, model: str) -> None:
        self.model = model

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        import litellm

        try:
            resp = await litellm.acompletion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = resp.choices[0].message.content
            return _extract_json_object(content)
        except Exception as exc:  # noqa: BLE001 — degrade to review queue, never crash sync
            logger.warning("LLM classification failed: {}", exc)
            return None


def build_classifier(llm_model: str | None) -> Classifier | None:
    return LiteLLMClassifier(llm_model) if llm_model else None
