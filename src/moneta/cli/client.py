"""Thin HTTP client: remote server if MONETA_API_URL is set, else in-process ASGI.

httpx.ASGITransport never fires FastAPI's lifespan, so the in-process branch
drives the app's own lifespan context manually around the request — one
engine, created by build_app() and disposed by its lifespan's shutdown.
"""

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import httpx
import typer
from fastapi import FastAPI
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
    app: FastAPI | None = None
    if settings.api_url:
        transport: httpx.AsyncBaseTransport | None = None
        base_url = settings.api_url
    else:
        from moneta.api import build_app

        app = build_app()
        transport = httpx.ASGITransport(app=app)
        base_url = "http://moneta.local"
    headers = {"Authorization": f"Bearer {settings.api_token}"} if settings.api_token else None
    async with AsyncExitStack() as stack:
        if app is not None:
            await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url=base_url, timeout=120)
        )
        try:
            resp = await client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            # remote mode: this is the CLI's own socket failing to reach the server.
            # in-process mode: no socket of ours is involved — the same exception
            # types propagate up THROUGH the ASGI app from an adapter's httpx call
            # (e.g. SimpleFIN unreachable), so name the aggregator, not a URL we
            # never had.
            target = settings.api_url or "a configured aggregator"
            console.print(f"[red]Error:[/red] could not reach {target} ({exc})")
            raise typer.Exit(1) from None
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
