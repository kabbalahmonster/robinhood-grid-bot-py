"""
RPC rotation and failover module for the Grid Trading Bot.

Provides round-robin rotation across multiple RPC endpoints with
automatic failover, health checking, and cooldown management.
Fully backward-compatible: if RPC_URLS is not set, falls back to
the single RPC_URL exactly as before.
"""

import time
import logging
import threading
from typing import Optional, List
from dataclasses import dataclass, field
from web3 import Web3

logger = logging.getLogger("grid_bot.rpc")

# Default public RPC endpoints for Robinhood Chain (chainId 4663)
DEFAULT_ROBINHOOD_RPCS = [
    "https://rpc.mainnet.chain.robinhood.com",
    "https://robinhood-rpc.publicnode.com",
    "https://robinhood.api.pocket.network",
]

# Default public RPC endpoints for Base (chainId 8453)
DEFAULT_BASE_RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.api.pocket.network",
]

# Default public RPC endpoints for Ethereum Mainnet (chainId 1)
DEFAULT_MAINNET_RPCS = [
    "https://eth-rpc.publicnode.com",
    "https://eth.api.pocket.network",
    "https://rpc.ankr.com/eth",
]

CHAIN_DEFAULT_RPCS = {
    4663: DEFAULT_ROBINHOOD_RPCS,
    8453: DEFAULT_BASE_RPCS,
    1: DEFAULT_MAINNET_RPCS,
}


@dataclass
class RPCEndpoint:
    """Tracks health and usage stats for a single RPC endpoint."""
    url: str
    is_healthy: bool = True
    last_failure: float = 0.0
    last_success: float = 0.0
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    
    # Cooldown after this many consecutive failures
    FAILURE_THRESHOLD: int = field(default=3, repr=False)
    # Seconds to wait before retrying a failed endpoint
    COOLDOWN_SECONDS: float = field(default=60.0, repr=False)
    
    def record_success(self):
        """Record a successful request."""
        self.is_healthy = True
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.total_requests += 1
    
    def record_failure(self):
        """Record a failed request."""
        self.consecutive_failures += 1
        self.total_failures += 1
        self.total_requests += 1
        self.last_failure = time.time()
        
        if self.consecutive_failures >= self.FAILURE_THRESHOLD:
            self.is_healthy = False
            logger.warning(
                f"RPC endpoint marked unhealthy after {self.consecutive_failures} "
                f"consecutive failures: {self.url}"
            )
    
    def should_retry(self) -> bool:
        """Check if a failed endpoint should be retried."""
        if self.is_healthy:
            return True
        # Allow retry after cooldown
        return (time.time() - self.last_failure) >= self.COOLDOWN_SECONDS
    
    @property
    def display_url(self) -> str:
        """Truncated URL for logging."""
        if len(self.url) > 50:
            return self.url[:30] + "..." + self.url[-15:]
        return self.url


class RPCRotator:
    """
    Manages multiple RPC endpoints with rotation and failover.
    
    Usage:
        rotator = RPCRotator(chain_id=4663, custom_rpcs=["https://..."])
        w3 = rotator.get_web3()  # Returns Web3 connected to next healthy endpoint
        
        # Or use as a context manager for automatic failover:
        with rotator.request() as w3:
            balance = w3.eth.get_balance(addr)
    """
    
    def __init__(
        self,
        chain_id: int,
        custom_rpcs: Optional[List[str]] = None,
        single_rpc_url: Optional[str] = None,
    ):
        """
        Initialize RPC rotator.
        
        Args:
            chain_id: Chain ID for default RPC selection.
            custom_rpcs: Optional list of custom RPC URLs (overrides defaults).
            single_rpc_url: If set, use only this URL (backward-compatible mode).
        """
        self.chain_id = chain_id
        self._lock = threading.Lock()
        self._current_index = 0
        self._web3_cache: dict[str, Web3] = {}
        
        # Build endpoint list
        if single_rpc_url:
            # Backward-compatible: single RPC mode (original behavior)
            self._endpoints = [RPCEndpoint(url=single_rpc_url)]
            self._rotation_enabled = False
            logger.info(f"RPC: single-endpoint mode ({single_rpc_url[:40]}...)")
        elif custom_rpcs:
            self._endpoints = [RPCEndpoint(url=url) for url in custom_rpcs]
            self._rotation_enabled = len(custom_rpcs) > 1
            logger.info(
                f"RPC: rotation mode with {len(custom_rpcs)} endpoints "
                f"(chain {chain_id})"
            )
        else:
            # Use chain defaults
            defaults = CHAIN_DEFAULT_RPCS.get(chain_id, [])
            if not defaults:
                raise ValueError(
                    f"No default RPCs for chain {chain_id}. "
                    f"Set RPC_URL or RPC_URLS in .env"
                )
            self._endpoints = [RPCEndpoint(url=url) for url in defaults]
            self._rotation_enabled = len(defaults) > 1
            logger.info(
                f"RPC: rotation mode with {len(defaults)} default endpoints "
                f"(chain {chain_id})"
            )
    
    @property
    def endpoint_count(self) -> int:
        return len(self._endpoints)
    
    @property
    def healthy_count(self) -> int:
        return sum(1 for e in self._endpoints if e.is_healthy)
    
    def _get_web3_for_url(self, url: str) -> Web3:
        """Get or create a cached Web3 instance for a URL."""
        if url not in self._web3_cache:
            self._web3_cache[url] = Web3(Web3.HTTPProvider(
                url,
                request_kwargs={"timeout": 15},
            ))
        return self._web3_cache[url]
    
    def get_web3(self) -> Web3:
        """
        Get a Web3 instance connected to the next healthy endpoint.
        
        Returns:
            Web3: Connected Web3 instance.
            
        Raises:
            ConnectionError: If no healthy endpoints are available.
        """
        with self._lock:
            if not self._rotation_enabled:
                # Single endpoint mode — just return it
                ep = self._endpoints[0]
                return self._get_web3_for_url(ep.url)
            
            # Try to find a healthy endpoint, starting from current index
            tried = 0
            while tried < len(self._endpoints):
                ep = self._endpoints[self._current_index]
                
                if ep.is_healthy or ep.should_retry():
                    # If retrying an unhealthy endpoint, log it
                    if not ep.is_healthy:
                        logger.info(f"Retrying cooled-down endpoint: {ep.display_url}")
                    
                    # Advance index for next call (round-robin)
                    self._current_index = (self._current_index + 1) % len(self._endpoints)
                    return self._get_web3_for_url(ep.url)
                
                # Skip unhealthy endpoint
                self._current_index = (self._current_index + 1) % len(self._endpoints)
                tried += 1
            
            # All endpoints unhealthy — force retry the first one
            logger.error("All RPC endpoints unhealthy! Forcing retry on first endpoint.")
            ep = self._endpoints[0]
            return self._get_web3_for_url(ep.url)
    
    def report_success(self, url: str):
        """Report a successful request to an endpoint."""
        for ep in self._endpoints:
            if ep.url == url:
                ep.record_success()
                break
    
    def report_failure(self, url: str, error: Optional[Exception] = None):
        """Report a failed request to an endpoint."""
        for ep in self._endpoints:
            if ep.url == url:
                ep.record_failure()
                if error:
                    logger.warning(f"RPC failure on {ep.display_url}: {error}")
                break
    
    def get_status(self) -> dict:
        """Get status of all endpoints."""
        return {
            "rotation_enabled": self._rotation_enabled,
            "total_endpoints": len(self._endpoints),
            "healthy_endpoints": self.healthy_count,
            "endpoints": [
                {
                    "url": ep.display_url,
                    "healthy": ep.is_healthy,
                    "consecutive_failures": ep.consecutive_failures,
                    "total_requests": ep.total_requests,
                    "total_failures": ep.total_failures,
                    "failure_rate": (
                        f"{(ep.total_failures / ep.total_requests * 100):.1f}%"
                        if ep.total_requests > 0 else "0%"
                    ),
                }
                for ep in self._endpoints
            ],
        }
    
    def log_status(self):
        """Log endpoint status summary."""
        status = self.get_status()
        logger.info(
            f"RPC Status: {status['healthy_endpoints']}/{status['total_endpoints']} "
            f"healthy, rotation={'ON' if status['rotation_enabled'] else 'OFF'}"
        )
        for ep in status["endpoints"]:
            if not ep["healthy"] or ep["total_failures"] > 0:
                logger.info(
                    f"  {'✅' if ep['healthy'] else '❌'} {ep['url']} "
                    f"| failures: {ep['total_failures']}/{ep['total_requests']} "
                    f"({ep['failure_rate']})"
                )


class ResilientWeb3:
    """
    A wrapper around Web3 that automatically handles RPC rotation and failover.
    
    Drop-in replacement for Web3 that retries failed requests on the next
    healthy endpoint. Use this in place of Web3(HTTPProvider(...)).
    
    Usage:
        w3 = ResilientWeb3(chain_id=4663, rpc_urls=["https://rpc1...", "https://rpc2..."])
        balance = w3.eth.get_balance(address)  # Auto-rotates on failure
    """
    
    # Maximum retries across endpoints before giving up
    MAX_RETRIES = 3
    
    def __init__(
        self,
        chain_id: int,
        rpc_urls: Optional[List[str]] = None,
        single_rpc_url: Optional[str] = None,
    ):
        """
        Initialize resilient Web3 wrapper.
        
        Args:
            chain_id: Chain ID for default RPC selection.
            rpc_urls: Optional list of RPC URLs for rotation.
            single_rpc_url: Single RPC URL (backward-compatible, no rotation).
        """
        self.rotator = RPCRotator(
            chain_id=chain_id,
            custom_rpcs=rpc_urls,
            single_rpc_url=single_rpc_url,
        )
        self._current_url: Optional[str] = None
        self._w3: Optional[Web3] = None
        self._refresh_connection()
    
    def _refresh_connection(self):
        """Get a fresh Web3 connection from the rotator."""
        self._w3 = self.rotator.get_web3()
        # Track which URL we're using
        if hasattr(self._w3.provider, 'endpoint_uri'):
            self._current_url = str(self._w3.provider.endpoint_uri)
        else:
            self._current_url = "unknown"
    
    def _execute_with_failover(self, func_name: str, *args, **kwargs):
        """
        Execute a Web3 method with automatic failover.
        
        Args:
            func_name: Dot-notation method path (e.g., "eth.get_balance").
            *args, **kwargs: Arguments to pass to the method.
            
        Returns:
            Method result.
        """
        last_error = None
        
        for attempt in range(self.MAX_RETRIES):
            try:
                # Navigate to the method (e.g., w3.eth.get_balance)
                obj = self._w3
                for part in func_name.split('.'):
                    obj = getattr(obj, part)
                
                result = obj(*args, **kwargs)
                
                # Success — report and return
                if self._current_url:
                    self.rotator.report_success(self._current_url)
                return result
            
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Check if it's a connection/rate-limit error worth failing over on
                is_retryable = any(x in error_str for x in [
                    "connection", "timeout", "429", "rate limit",
                    "too many requests", "503", "502", "500",
                    "internal error", "server error",
                ])
                
                if self._current_url:
                    self.rotator.report_failure(self._current_url, e)
                
                if not is_retryable and attempt == 0:
                    # Non-retryable error (e.g., invalid params) — don't retry
                    raise
                
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        f"RPC request failed (attempt {attempt + 1}/{self.MAX_RETRIES}), "
                        f"switching endpoint: {e}"
                    )
                    self._refresh_connection()
                    time.sleep(0.5 * (attempt + 1))  # Brief backoff
        
        raise ConnectionError(
            f"All RPC endpoints failed after {self.MAX_RETRIES} attempts. "
            f"Last error: {last_error}"
        )
    
    @property
    def eth(self):
        """Proxy to w3.eth with failover."""
        return _ResilientNamespace(self, "eth")
    
    def is_connected(self) -> bool:
        """Check if current connection is alive."""
        try:
            return self._w3.is_connected()
        except Exception:
            return False
    
    def get_status(self) -> dict:
        """Get RPC endpoint status."""
        return self.rotator.get_status()
    
    def log_status(self):
        """Log RPC endpoint status."""
        self.rotator.log_status()


class _ResilientNamespace:
    """Proxy for Web3 namespaces (eth, net, etc.) with failover."""
    
    def __init__(self, resilient_w3: ResilientWeb3, namespace: str):
        self._rw3 = resilient_w3
        self._ns = namespace
    
    def __getattr__(self, method_name: str):
        func_path = f"{self._ns}.{method_name}"
        
        # For properties (not callable), get them directly
        attr = getattr(self._rw3._w3.eth, method_name, None)
        if attr is not None and not callable(attr):
            return attr
        
        # For methods, wrap with failover
        def wrapper(*args, **kwargs):
            return self._rw3._execute_with_failover(func_path, *args, **kwargs)
        
        return wrapper


def create_web3(config) -> Web3:
    """
    Factory function to create the appropriate Web3 instance based on config.
    
    If config has rpc_urls list with multiple entries, returns ResilientWeb3.
    Otherwise returns standard Web3 (backward-compatible).
    
    Args:
        config: BotConfig instance.
        
    Returns:
        Web3 or ResilientWeb3 instance.
    """
    rpc_urls = getattr(config, 'rpc_urls', None)
    
    if rpc_urls and len(rpc_urls) > 1:
        logger.info(f"Creating resilient Web3 with {len(rpc_urls)} RPC endpoints")
        return ResilientWeb3(
            chain_id=config.chain_id,
            rpc_urls=rpc_urls,
        )
    else:
        # Backward-compatible: single RPC
        rpc_url = config.rpc_url
        logger.info(f"Creating standard Web3 with single RPC: {rpc_url[:40]}...")
        return Web3(Web3.HTTPProvider(
            rpc_url,
            request_kwargs={"timeout": 15},
        ))
