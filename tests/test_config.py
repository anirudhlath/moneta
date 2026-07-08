from pathlib import Path

from pydantic_settings import BaseSettings

from moneta.config import Settings, load_settings, save_config_value


def test_defaults(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    s = load_settings()
    assert s.db_path == tmp_path / "moneta.db"
    assert s.simplefin_access_url is None
    assert s.llm_model is None
    assert s.api_url is None


def test_env_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("MONETA_LLM_MODEL", "anthropic/claude-haiku-4-5")
    assert load_settings().llm_model == "anthropic/claude-haiku-4-5"


def test_save_and_reload(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    save_config_value("simplefin_access_url", "https://u:p@bridge.example/simplefin")
    assert load_settings().simplefin_access_url == "https://u:p@bridge.example/simplefin"


def test_settings_is_pydantic() -> None:
    assert issubclass(Settings, BaseSettings)
