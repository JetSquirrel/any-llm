from typing import Any

import httpx
from pydantic import BaseModel

from any_llm.gateway.config import GuardrailPluginConfig, GuardrailsConfig
from any_llm.gateway.log_config import logger


class GuardrailCheckResult(BaseModel):
    """Result from a guardrail plugin check."""

    allowed: bool
    reason: str | None = None
    score: float | None = None
    plugin_name: str | None = None


class GuardrailsChecker:
    """Checker for guardrail plugins that validates request content."""

    def __init__(self, config: GuardrailsConfig) -> None:
        """Initialize the guardrails checker.

        Args:
            config: Guardrails configuration
        """
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call_plugin(
        self,
        plugin: GuardrailPluginConfig,
        request_body: dict[str, Any],
    ) -> GuardrailCheckResult:
        """Call a single guardrail plugin.

        Args:
            plugin: Plugin configuration
            request_body: Request body to check

        Returns:
            GuardrailCheckResult with the plugin's response
        """
        client = await self._get_client()

        try:
            response = await client.post(
                plugin.url,
                json=request_body,
                timeout=plugin.timeout,
            )
            response.raise_for_status()
            data = response.json()

            return GuardrailCheckResult(
                allowed=data.get("allowed", True),
                reason=data.get("reason"),
                score=data.get("score"),
                plugin_name=plugin.name,
            )

        except httpx.TimeoutException:
            logger.warning(f"Guardrail plugin '{plugin.name}' timed out after {plugin.timeout}s")
            if self.config.fail_open:
                return GuardrailCheckResult(
                    allowed=True,
                    reason="Plugin timeout (fail-open mode)",
                    plugin_name=plugin.name,
                )
            return GuardrailCheckResult(
                allowed=False,
                reason="Plugin timeout (fail-closed mode)",
                plugin_name=plugin.name,
            )

        except httpx.HTTPStatusError as e:
            logger.warning(f"Guardrail plugin '{plugin.name}' returned HTTP error: {e.response.status_code}")
            if self.config.fail_open:
                return GuardrailCheckResult(
                    allowed=True,
                    reason=f"Plugin HTTP error {e.response.status_code} (fail-open mode)",
                    plugin_name=plugin.name,
                )
            return GuardrailCheckResult(
                allowed=False,
                reason=f"Plugin HTTP error {e.response.status_code} (fail-closed mode)",
                plugin_name=plugin.name,
            )

        except Exception as e:
            logger.warning(f"Guardrail plugin '{plugin.name}' failed with error: {e}")
            if self.config.fail_open:
                return GuardrailCheckResult(
                    allowed=True,
                    reason=f"Plugin error (fail-open mode): {e}",
                    plugin_name=plugin.name,
                )
            return GuardrailCheckResult(
                allowed=False,
                reason=f"Plugin error (fail-closed mode): {e}",
                plugin_name=plugin.name,
            )

    async def check(self, request_body: dict[str, Any]) -> GuardrailCheckResult:
        """Check request against all enabled guardrail plugins.

        Args:
            request_body: The request body to check (typically contains 'messages')

        Returns:
            GuardrailCheckResult indicating whether the request is allowed
        """
        if not self.config.enabled:
            return GuardrailCheckResult(allowed=True, reason="Guardrails disabled")

        enabled_plugins = [p for p in self.config.plugins if p.enabled]

        if not enabled_plugins:
            return GuardrailCheckResult(allowed=True, reason="No enabled plugins")

        fail_open_results: list[GuardrailCheckResult] = []

        for plugin in enabled_plugins:
            result = await self._call_plugin(plugin, request_body)

            if not result.allowed:
                logger.info(
                    f"Request blocked by guardrail plugin '{plugin.name}': {result.reason}"
                )
                return result

            # Track fail-open results (when allowed=True due to plugin failure)
            if result.reason and ("fail-open" in result.reason):
                fail_open_results.append(result)

        # If any plugins failed but were allowed due to fail-open, return that info
        if fail_open_results:
            return fail_open_results[0]

        return GuardrailCheckResult(allowed=True, reason="All plugins passed")
