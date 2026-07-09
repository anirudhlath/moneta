"""LLM boundary. Classification only — never arithmetic, never the money path."""

import json
import re
from typing import Any, Protocol

from loguru import logger

# Some providers (notably OpenRouter routes) ignore response_format and fence the JSON.
_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$")


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip())


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
            result: dict[str, Any] = json.loads(_strip_fences(content))
            return result
        except Exception as exc:  # noqa: BLE001 — degrade to review queue, never crash sync
            logger.warning("LLM classification failed: {}", exc)
            return None


def build_classifier(llm_model: str | None) -> Classifier | None:
    return LiteLLMClassifier(llm_model) if llm_model else None
