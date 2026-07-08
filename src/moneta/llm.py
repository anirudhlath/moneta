"""LLM boundary. Classification only — never arithmetic, never the money path."""

import json
from typing import Any, Protocol

from loguru import logger


class Classifier(Protocol):
    async def classify_json(self, prompt: str) -> dict[str, Any] | None: ...


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
            result: dict[str, Any] = json.loads(content)
            return result
        except Exception as exc:  # noqa: BLE001 — degrade to review queue, never crash sync
            logger.warning("LLM classification failed: {}", exc)
            return None


def build_classifier(llm_model: str | None) -> Classifier | None:
    return LiteLLMClassifier(llm_model) if llm_model else None
