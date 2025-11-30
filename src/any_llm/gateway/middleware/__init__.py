import json
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from any_llm.gateway.config import GuardrailsConfig
from any_llm.gateway.guardrails import GuardrailsChecker
from any_llm.gateway.log_config import logger


class GuardrailsMiddleware(BaseHTTPMiddleware):
    """Middleware that checks requests against guardrail plugins."""

    PROTECTED_PATHS: ClassVar[set[str]] = {"/v1/chat/completions", "/v1/completions"}

    def __init__(self, app: Any, config: GuardrailsConfig) -> None:
        """Initialize the guardrails middleware.

        Args:
            app: The ASGI application
            config: Guardrails configuration
        """
        super().__init__(app)
        self.checker = GuardrailsChecker(config)
        self.config = config

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Process the request through guardrail checks.

        Args:
            request: The incoming request
            call_next: The next middleware/handler in the chain

        Returns:
            Response from the handler or an error response if blocked
        """
        if not self.config.enabled:
            return await call_next(request)

        path = request.url.path
        if path not in self.PROTECTED_PATHS:
            return await call_next(request)

        if request.method != "POST":
            return await call_next(request)

        try:
            body_bytes = await request.body()
            if not body_bytes:
                return await call_next(request)

            try:
                body: dict[str, Any] = json.loads(body_bytes)
            except json.JSONDecodeError:
                return await call_next(request)

            result = await self.checker.check(body)

            if not result.allowed:
                logger.warning(
                    f"Request to {path} blocked by guardrails: {result.reason}"
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": f"Request blocked by security policy: {result.reason}",
                            "type": "guardrail_violation",
                            "plugin": result.plugin_name,
                            "score": result.score,
                        }
                    },
                )

        except Exception as e:
            logger.error(f"Error in guardrails middleware: {e}")
            if not self.config.fail_open:
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "message": "Internal guardrails error (fail-closed mode)",
                            "type": "guardrail_error",
                        }
                    },
                )

        return await call_next(request)
