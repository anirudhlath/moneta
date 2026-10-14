from typing import Any

import litellm
import pytest

from moneta.llm import LiteLLMClassifier, build_classifier


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


def _fake_acompletion(content: str) -> Any:
    async def fake(**kwargs: Any) -> _Resp:
        return _Resp(content)

    return fake


async def test_plain_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion('{"merchant": "Blue Bottle"}'))
    result = await LiteLLMClassifier("openrouter/x").classify_json("prompt")
    assert result == {"merchant": "Blue Bottle"}


async def test_markdown_fenced_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    # OpenRouter-routed models often ignore response_format and fence their JSON
    monkeypatch.setattr(
        litellm, "acompletion", _fake_acompletion('```json\n{"merchant": "Blue Bottle"}\n```')
    )
    result = await LiteLLMClassifier("openrouter/x").classify_json("prompt")
    assert result == {"merchant": "Blue Bottle"}


async def test_bare_fence_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion('```\n{"ok": true}\n```'))
    result = await LiteLLMClassifier("openrouter/x").classify_json("prompt")
    assert result == {"ok": True}


async def test_non_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion("Sure! The merchant is X."))
    assert await LiteLLMClassifier("openrouter/x").classify_json("prompt") is None


async def test_provider_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**kwargs: Any) -> _Resp:
        raise RuntimeError("provider down")

    monkeypatch.setattr(litellm, "acompletion", boom)
    assert await LiteLLMClassifier("openrouter/x").classify_json("prompt") is None


async def test_trailing_prose_after_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        _fake_acompletion('{"is_recurring": true, "confident": true}\n\nThis is clearly a bill.'),
    )
    result = await LiteLLMClassifier("openrouter/x").classify_json("prompt")
    assert result == {"is_recurring": True, "confident": True}


async def test_leading_prose_before_json_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        _fake_acompletion(
            'Here is my answer:\n```json\n{"merchant": "Acme"}\n```\nHope that helps!'
        ),
    )
    result = await LiteLLMClassifier("openrouter/x").classify_json("prompt")
    assert result == {"merchant": "Acme"}


async def test_api_base_reaches_litellm_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> _Resp:
        calls.append(kwargs)
        return _Resp('{"ok": true}')

    monkeypatch.setattr(litellm, "acompletion", fake)
    await build_classifier("hosted_vllm/m", "http://localhost:8000/v1").classify_json("p")  # type: ignore[union-attr]
    await build_classifier("openrouter/x", None).classify_json("p")  # type: ignore[union-attr]
    assert calls[0]["api_base"] == "http://localhost:8000/v1"
    assert "api_base" not in calls[1]
    assert build_classifier(None, "http://localhost:8000/v1") is None
