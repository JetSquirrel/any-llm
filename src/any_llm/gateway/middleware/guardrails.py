"""Guardrails middleware for content filtering and security scanning."""

import json
import logging
from typing import Any, Callable

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from any_llm.gateway.config import GuardrailsConfig, GuardrailPlugin

logger = logging.getLogger("gateway.guardrails")


class GuardrailsMiddleware:
    """Pure ASGI middleware to scan requests through configured guardrail plugins."""

    def __init__(self, app: ASGIApp, config: GuardrailsConfig):
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Only scan chat completion endpoints
        path = scope.get("path", "")
        if not path.startswith("/v1/chat/completions"):
            await self.app(scope, receive, send)
            return

        if not self.config.enabled:
            await self.app(scope, receive, send)
            return

        # Collect request body
        body_parts: list[bytes] = []
        
        async def receive_wrapper() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                body_parts.append(body)
            return message

        # Read entire body first
        while True:
            message = await receive_wrapper()
            if message["type"] == "http.request":
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return

        body = b"".join(body_parts)

        # Check body size
        if len(body) > self.config.max_body_size:
            logger.warning(f"Request body too large: {len(body)} bytes")
            if not self.config.fail_open:
                response = JSONResponse(status_code=413, content={"error": "Request body too large"})
                await response(scope, receive, send)
                return

        # Parse JSON
        try:
            request_data = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse request body: {e}")
            if not self.config.fail_open:
                response = JSONResponse(status_code=400, content={"error": "Invalid request body"})
                await response(scope, receive, send)
                return
            request_data = {}

        # Scan through plugins if we have valid data
        if request_data is not None:
            for plugin in self.config.plugins:
                result = await self._scan_with_plugin(plugin, request_data)
                if result is None:
                    if not self.config.fail_open:
                        response = JSONResponse(
                            status_code=503,
                            content={"error": f"Guardrail service unavailable: {plugin.name}"},
                        )
                        await response(scope, receive, send)
                        return
                    continue

                if not result.get("allowed", True):
                    logger.warning(f"Guardrail {plugin.name} blocked: {result.get('reason')}")
                    response = JSONResponse(
                        status_code=400,
                        content={
                            "error": "Request blocked by guardrails",
                            "reason": result.get("reason", "Content policy violation"),
                            "guardrail": plugin.name,
                        },
                    )
                    await response(scope, receive, send)
                    return

        # Create new receive that returns the cached body
        body_sent = False

        async def receive_with_body() -> Message:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            # After body is sent, wait for disconnect
            return await receive()

        await self.app(scope, receive_with_body, send)

    async def _scan_with_plugin(self, plugin: GuardrailPlugin, data: dict[str, Any]) -> dict[str, Any] | None:
        """Scan request with a guardrail plugin. Returns None on failure."""
        async with httpx.AsyncClient(timeout=plugin.timeout) as client:
            try:
                logger.info(f"Scanning with {plugin.name} at {plugin.url}")
                data["guard_model"] = plugin.guard_model
                data["prvider"] = "openai"
                data["model"] = "openai:deepseek-chat"

                logger.info(f"Request data for {plugin.name}: {data}")
                response = await client.post(plugin.url, json=data or {})
                logger.info(f"Response status: {response.status_code}")
                response.raise_for_status()
                result = response.json()
                logger.info(f"Guardrail {plugin.name} response: {result}")
                return result
            except httpx.HTTPStatusError as e:
                logger.error(f"Guardrail {plugin.name} HTTP {e.response.status_code}: {e.response.text}")
                return None
            except Exception as e:
                logger.error(f"Guardrail {plugin.name} failed: {type(e).__name__}: {e}")
                return None
