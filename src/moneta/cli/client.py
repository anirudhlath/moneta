"""Thin HTTP client: remote server if MONETA_API_URL is set, else in-process ASGI."""

import asyncio
from typing import Any

import httpx
import typer
from rich.console import Console

from moneta.config import load_settings

console = Console()


async def _arequest(
    method: str,
    path: str,
    json_body: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> Any:
    settings = load_settings()
    if settings.api_url:
        transport: httpx.AsyncBaseTransport | None = None
        base_url = settings.api_url
    else:
        from moneta.api import build_app
        from moneta.db import init_db, make_sessionmaker

        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        engine, _ = make_sessionmaker(f"sqlite+aiosqlite:///{settings.db_path}")
        await init_db(engine)
        await engine.dispose()
        transport = httpx.ASGITransport(app=build_app())
        base_url = "http://moneta.local"
    headers = {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else None
    async with httpx.AsyncClient(transport=transport, base_url=base_url, timeout=120) as client:
        resp = await client.request(method, path, params=params, json=json_body, headers=headers)
    if resp.status_code >= 400:
        try:  # proxies and unhandled 500s return plaintext/HTML, not FastAPI's JSON
            body = resp.json()
            detail = body.get("detail", resp.text) if isinstance(body, dict) else resp.text
        except ValueError:
            detail = resp.text
        console.print(f"[red]Error:[/red] {detail}")
        raise typer.Exit(1)
    return resp.json()


def request(
    method: str,
    path: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    return asyncio.run(_arequest(method, path, json_body, params))
