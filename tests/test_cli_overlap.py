import json
from typing import Any

import pytest
from typer.testing import CliRunner

from moneta.cli.main import app

runner = CliRunner()


@pytest.mark.parametrize(
    "args,expected",
    [
        (["accounts", "--sources", "--json"], [("GET", "/accounts", None, {"sources": True})]),
        (
            ["accounts", "--link-source", "2", "--to", "1"],
            [
                ("POST", "/accounts/2/link", {"canonical_account_id": 1}, None),
                ("GET", "/accounts", None, None),
            ],
        ),
        (
            ["accounts", "--unlink-source", "2"],
            [("POST", "/accounts/2/unlink", None, None), ("GET", "/accounts", None, None)],
        ),
        (["txns", "--sources", "--json"], [("GET", "/transactions", None, {"sources": True})]),
        (["reconcile", "--json"], [("POST", "/reconcile", None, None)]),
    ],
)
def test_overlap_commands_send_exact_contract(
    monkeypatch: pytest.MonkeyPatch, args: list[str], expected: list[tuple[str, str, Any, Any]]
) -> None:
    calls: list[tuple[str, str, Any, Any]] = []

    def transport(method: str, path: str, json_body: Any = None, params: Any = None) -> Any:
        calls.append((method, path, json_body, params))
        return [] if path == "/accounts" else {"transactions": [], "accounts_linked": 1}

    monkeypatch.setattr("moneta.cli.main.request", transport)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert calls == expected
    if args == ["reconcile", "--json"]:
        assert json.loads(result.output)["accounts_linked"] == 1


@pytest.mark.parametrize(
    "flags",
    [
        ["--link-source", "2"],
        ["--to", "1"],
        ["--link-source", "2", "--to", "1", "--unlink-source", "2"],
        ["--link-source", "2", "--to", "1", "--set-type", "2", "checking"],
        ["--unlink-source", "2", "--json"],
        ["--link-source", "2", "--to", "1", "--json"],
        ["--set-type", "2", "bogus", "--set-promo", "2", "2026-12-01"],
        ["--set-type", "2", "checking", "--set-financing", "2", "bogus"],
        ["--link-source", "0", "--to", "1"],
        ["--unlink-source", "-1"],
    ],
)
def test_invalid_account_flags_fail_before_any_request(
    monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> None:
    calls: list[str] = []

    def transport(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append("request")
        return []

    monkeypatch.setattr("moneta.cli.main.request", transport)
    result = runner.invoke(app, ["accounts", *flags])
    assert result.exit_code != 0
    assert not calls


@pytest.mark.parametrize(
    "kind,key,answer,selected",
    [
        ("account_overlap", "canonical_account_id", "1", 42),
        ("account_overlap", "canonical_account_id", "0", None),
        ("transaction_overlap", "duplicate_of_id", "1", 42),
        ("transaction_overlap", "duplicate_of_id", "0", None),
    ],
)
def test_review_numbered_overlap_candidates(
    monkeypatch: pytest.MonkeyPatch, kind: str, key: str, answer: str, selected: int | None
) -> None:
    resolutions: list[Any] = []

    def transport(method: str, path: str, json_body: Any = None, params: Any = None) -> Any:
        if method == "GET":
            return [
                {
                    "id": 3,
                    "kind": kind,
                    "question": "Same provider observation?",
                    "payload": {},
                    "context": {
                        "candidates": [
                            {
                                "id": 42,
                                "name": "Checking",
                                "source": "plaid",
                                "mask": "1234",
                                "posted_on": "2026-10-01",
                                "amount_cents": -1000,
                                "description": "Coffee",
                                "account": "Checking",
                            }
                        ]
                    },
                }
            ]
        resolutions.append((path, json_body))
        return {"ok": True}

    monkeypatch.setattr("moneta.cli.main.request", transport)
    result = runner.invoke(app, ["review"], input=answer + "\n")
    assert result.exit_code == 0, result.output
    assert resolutions == [("/review/3/resolve", {"resolution": {key: selected}})]
    assert "plaid" in result.output
    assert "none" in result.output
