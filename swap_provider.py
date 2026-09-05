"""Common swap-provider capabilities and construction.

This module centralizes provider selection and the small behavioral differences
that previously leaked throughout ``grid_bot.py``. Existing client classes keep
their public APIs; the adapter delegates calls while exposing explicit
capabilities to the trading engine.
"""

from dataclasses import dataclass
from importlib import import_module
import logging
import re


@dataclass(frozen=True)
class ProviderCapabilities:
    price_requires_taker: bool = False
    refresh_after_approval: bool = False
    api_managed_approval: bool = False
    quote_requires_preparation: bool = False


class SwapProvider:
    """Thin compatibility adapter around an existing provider client."""

    def __init__(self, name, client, capabilities):
        self.name = name
        self.client = client
        self.capabilities = capabilities

    def __getattr__(self, name):
        return getattr(self.client, name)

    def prepare_swap(self, quote):
        """Convert a quote into executable transaction fields when required."""
        if not self.capabilities.quote_requires_preparation:
            return quote
        return self.client.get_swap_transaction(quote.raw_response)


class FallbackSwapProvider:
    """Retry complete provider operations with a secondary provider.

    Provider methods only mark a retryable failure. ``run_with_fallback`` then
    lets the current operation unwind completely before rerunning it from the
    beginning with the fallback. This keeps quotes, approvals, and calldata
    provider-consistent while avoiding a one-poll delay.
    """

    # 409 is normally a semantic conflict, but provider gateways also use it
    # for transient upstream/proxy failures (for example Uniswap's
    # "client packet length exceeds 255 buffer"). Those failures are not
    # actionable by the bot and must fail over instead of freezing pricing.
    RETRYABLE_STATUS_CODES = {404, 408, 409, 425, 429, 500, 502, 503, 504}
    RETRYABLE_ERROR_MARKERS = (
        "connection error",
        "connection reset",
        "connection aborted",
        "request failed",
        "timed out",
        "timeout",
        "temporarily unavailable",
        # Shared provider coordinators intentionally reject requests locally
        # while an upstream is quarantined. That is still a retryable primary
        # failure: the complete operation must immediately use the configured
        # fallback rather than surfacing "Could not get price".
        "cooldown active",
    )

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.active = primary
        self._operation_retries = []
        self._operation_sealed = []
        self.logger = logging.getLogger("grid_bot.swap_provider")

    @property
    def name(self):
        return self.active.name

    @property
    def capabilities(self):
        return self.active.capabilities

    @property
    def client(self):
        return self.active.client

    @property
    def fallback_active(self):
        return self.active is self.fallback

    def run_with_fallback(self, operation, operation_name="swap operation"):
        """Run one complete operation with primary, then fallback if marked."""
        previous_active = self.active
        try:
            self.active = self.primary
            result, retry = self._run_operation_attempt(operation)
            if not retry:
                return result

            self.logger.warning(
                f"Retrying {operation_name} from the beginning with "
                f"{self.fallback.name}; no swap transaction was broadcast."
            )
            self.active = self.fallback
            result, _ = self._run_operation_attempt(operation)
            return result
        finally:
            self.active = previous_active if self._operation_retries else self.primary

    def _run_operation_attempt(self, operation):
        self._operation_retries.append(None)
        self._operation_sealed.append(False)
        try:
            result = operation()
            return result, self._operation_retries[-1]
        finally:
            self._operation_retries.pop()
            self._operation_sealed.pop()

    def seal_current_operation(self):
        """Disable provider failover after this operation broadcasts on-chain."""
        if self._operation_sealed:
            self._operation_sealed[-1] = True

    def recover_current_operation(self):
        """Cancel a pending provider replay after same-provider recovery.

        A higher-level operation may recover from a retryable native-route
        failure by finding a direct WETH route with the same provider.  Once
        that recovery succeeds, the earlier failure must not cause the whole
        operation to replay with the fallback provider after settlement.
        """
        if self._operation_retries and not self._operation_sealed[-1]:
            self._operation_retries[-1] = None

    def __getattr__(self, method_name):
        method = getattr(self.active, method_name)
        if not callable(method):
            return method

        def guarded_call(*args, **kwargs):
            result = method(*args, **kwargs)
            # A retry can only be requested while a complete operation is
            # explicitly running under run_with_fallback. Direct transaction
            # calls must fail closed on the primary provider.
            if self._operation_retries:
                self._request_retry_after_failure(method_name, result)
            return result

        return guarded_call

    def prepare_swap(self, quote):
        result = self.active.prepare_swap(quote)
        self._request_retry_after_failure("prepare_swap", result)
        return result

    def _request_retry_after_failure(self, method_name, result):
        if (
            self.active is not self.primary
            or (self._operation_sealed and self._operation_sealed[-1])
            or not self._is_retryable_failure(result)
        ):
            return
        original_error = self._error_text(result)
        message = (
            f"{self.primary.name} {method_name} failed with a retryable error; "
            f"the current operation will stop safely and retry immediately "
            f"with {self.fallback.name}."
        )
        self.logger.warning(f"{message} Error: {original_error}")
        if self._operation_retries:
            self._operation_retries[-1] = (method_name, original_error)
        if hasattr(result, "error"):
            result.error = f"{original_error}; {message}"
        elif isinstance(result, dict):
            result["error"] = f"{original_error}; {message}"
            result["fallback_requested"] = True

    @classmethod
    def _is_retryable_failure(cls, result):
        if result is None:
            return True
        if hasattr(result, "success") and result.success:
            return False
        if isinstance(result, dict) and "error" not in result:
            return False
        error = cls._error_text(result).lower()
        status_codes = {int(code) for code in re.findall(r"\b(\d{3})\b", error)}
        if status_codes & cls.RETRYABLE_STATUS_CODES or any(500 <= code <= 599 for code in status_codes):
            return True
        return any(marker in error for marker in cls.RETRYABLE_ERROR_MARKERS)

    @staticmethod
    def _error_text(result):
        if result is None:
            return "provider returned no result"
        if isinstance(result, dict):
            return str(result.get("error") or result.get("detail") or result)
        return str(getattr(result, "error", result))


@dataclass(frozen=True)
class ProviderDefinition:
    module: str
    client_class: str
    capabilities: ProviderCapabilities

    def load_client_class(self):
        return getattr(import_module(self.module), self.client_class)


PROVIDERS = {
    "0x": ProviderDefinition("zero_x", "ZeroXClient", ProviderCapabilities()),
    "lifi": ProviderDefinition("li_fi", "LiFiClient", ProviderCapabilities(
        price_requires_taker=True,
        refresh_after_approval=True,
    )),
    "uniswap": ProviderDefinition("uniswap_api", "UniswapAPIClient", ProviderCapabilities(
        price_requires_taker=True,
        api_managed_approval=True,
        quote_requires_preparation=True,
    )),
    "sushiswap": ProviderDefinition("sushi_api", "SushiAPIClient", ProviderCapabilities(
        refresh_after_approval=True,
    )),
}


def resolve_provider_name(config):
    """Resolve explicit provider name, preserving legacy flag precedence."""
    explicit = (getattr(config, "swap_provider", "") or "").strip().lower()
    aliases = {"li.fi": "lifi", "li_fi": "lifi", "zero_x": "0x", "zerox": "0x", "sushi": "sushiswap"}
    explicit = aliases.get(explicit, explicit)
    if explicit:
        return explicit
    if getattr(config, "use_uniswap_api", False):
        return "uniswap"
    if getattr(config, "use_li_fi", False):
        return "lifi"
    return "0x"


def create_swap_provider(config):
    name = resolve_provider_name(config)
    if name not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported SWAP_PROVIDER '{name}'. Supported: {supported}")
    definition = PROVIDERS[name]
    client_class = definition.load_client_class()
    primary = SwapProvider(name, client_class(config), definition.capabilities)

    fallback_name = (getattr(config, "swap_fallback_provider", "") or "").strip().lower()
    aliases = {"li.fi": "lifi", "li_fi": "lifi", "zero_x": "0x", "zerox": "0x", "sushi": "sushiswap"}
    fallback_name = aliases.get(fallback_name, fallback_name)
    # The default fallback value is sushiswap. When Sushi itself is primary,
    # make the pair symmetric if Uniswap credentials are available. An empty
    # SWAP_FALLBACK_PROVIDER still disables fallback explicitly.
    if fallback_name == name == "sushiswap" and getattr(config, "uniswap_api_key", ""):
        fallback_name = "uniswap"
    if not fallback_name or fallback_name == name:
        return primary
    if fallback_name not in PROVIDERS:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported SWAP_FALLBACK_PROVIDER '{fallback_name}'. Supported: {supported}")

    fallback_definition = PROVIDERS[fallback_name]
    fallback_class = fallback_definition.load_client_class()
    fallback = SwapProvider(
        fallback_name,
        fallback_class(config),
        fallback_definition.capabilities,
    )
    logging.getLogger("grid_bot.swap_provider").info(
        f"Swap fallback enabled: {name} -> {fallback_name}"
    )
    return FallbackSwapProvider(primary, fallback)
