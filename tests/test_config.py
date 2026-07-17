import tomllib
from pathlib import Path

import pytest
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


def test_plaid_settings_default_and_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    s = load_settings()
    assert s.plaid_client_id is None
    assert s.plaid_secret is None
    assert s.plaid_env == "production"
    save_config_value("plaid_client_id", "cid")
    save_config_value("plaid_secret", "sec")
    save_config_value("plaid_env", "sandbox")
    s = load_settings()
    assert (s.plaid_client_id, s.plaid_secret, s.plaid_env) == ("cid", "sec", "sandbox")
    monkeypatch.setenv("MONETA_PLAID_ENV", "production")
    assert load_settings().plaid_env == "production"


def test_save_config_value_escapes_and_roundtrips(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    tricky = 'https://u:p"w\\x@bridge.example/simplefin'
    save_config_value("simplefin_access_url", tricky)
    assert load_settings().simplefin_access_url == tricky


def test_save_config_value_restricts_permissions(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    save_config_value("llm_model", "gpt")
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_ntfy_topic_default_and_env_override(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    assert load_settings().ntfy_topic is None
    monkeypatch.setenv("MONETA_NTFY_TOPIC", "https://ntfy.sh/moneta-xyz123")
    assert load_settings().ntfy_topic == "https://ntfy.sh/moneta-xyz123"


def test_malformed_config_file_raises_toml_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MONETA_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text("not = = valid\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_settings()
