"""
Configuration module for the Robinhood Chain Grid Trading Bot.

Loads environment variables and provides validated configuration settings
for bot operation across different EVM chains.
"""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Contract addresses for supported chains
CHAIN_CONFIG = {
    4663: {  # Robinhood Chain
        "name": "Robinhood",
        "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "zero_x_proxy": "0x0000000000001ff3684f28c67538d4d072c22734",  # 0x AllowanceHolder
        "uniswap_router": "0x8876789976decbfcbbbe364623c63652db8c0904",  # Universal Router v2.1.1
        "default_max_positions": 20,
    },
    8453: {  # Base
        "name": "Base",
        "weth": "0x4200000000000000000000000000000000000006",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "zero_x_proxy": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
        "uniswap_router": "0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B",  # Universal Router v2
        "default_max_positions": 10,
    },
    1: {  # Ethereum Mainnet
        "name": "Mainnet",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "permit2": "0x000000000022d473030f116ddee9f6b43ac78ba3",
        "zero_x_proxy": "0xDef1C0ded9bec7F1a1670819833240f027b25EfF",
        "uniswap_router": "0xEf1c6E67703c7BD7107eed8303Fbe6EC2554BF6B",  # Universal Router v2
        "default_max_positions": 10,
    },
}

# 0x API base URLs by chain
ZEROX_API_URLS = {
    4663: "https://api.0x.org",  # Robinhood uses standard 0x API
    8453: "https://api.0x.org",
    1: "https://api.0x.org",
}


def _parse_rpc_urls(env_value: str) -> Optional[list]:
    """
    Parse RPC_URLS environment variable into a list of URLs.
    
    Supports comma-separated format:
        RPC_URLS=https://rpc1.example.com,https://rpc2.example.com
    
    Returns None if empty or not set (falls back to single RPC_URL mode).
    """
    if not env_value or not env_value.strip():
        return None
    urls = [url.strip() for url in env_value.split(",") if url.strip()]
    return urls if urls else None


@dataclass
class BotConfig:
    """
    Configuration dataclass for the grid trading bot.
    
    Contains all settings required for bot operation including
    blockchain connection, trading parameters, and API keys.
    """
    
    # Wallet and Connection
    private_key: str
    rpc_url: str
    chain_id: int
    
    # API Keys
    zero_x_api_key: str
    
    # Token Addresses
    token_address: str
    token_symbol: str
    weth_address: str
    usdg_address: str
    treasury_allowed_recipients: list[str]
    permit2_address: str
    zero_x_proxy: str
    
    # Trading Parameters
    grid_spacing_percent: float
    max_positions: int
    max_active_positions: int
    min_profit_percent: float
    initial_buy_amount: float
    slippage_tolerance: float
    bank_percentage: float
    moonbag_percentage: float
    bank_min_amount: float
    fast_profit: bool
    tradeable_balance_percent: float
    
    # Bot Behavior
    poll_interval_seconds: int
    anti_mev_jitter: bool
    log_level: str
    state_file: str
    compact_mode: bool
    minimal_logs: bool
    
    # API Provider Selection
    swap_provider: str  # Explicit provider: 0x, lifi, uniswap, or sushiswap
    sushi_api_key: str  # Optional Sushi API key for higher service limits
    use_li_fi: bool  # If True, use LI.FI instead of 0x
    li_fi_api_key: str
    li_fi_integrator: str  # Required integrator string for LI.FI API
    
    use_uniswap_api: bool  # If True, use Uniswap API
    uniswap_api_key: str
    uniswap_permit2_disabled: bool  # Set to True to disable Permit2
    uniswap_router: str  # Universal Router address for token approvals
    
    # Gridless Trading Mode
    use_gridless: bool  # If True, use gridless position-based trading
    gridless_buy_threshold: float  # P&L % to trigger new buy (default: -10.0)
    gridless_sell_threshold: float  # P&L % to trigger sell (default: 5.0)
    gridless_stoploss_threshold: float  # P&L % for stoploss (default: -25.0)
    gridless_stoploss_enabled: bool  # Enable stoploss sells
    gridless_leading_edge: bool  # Enable leading edge buys (buy into strength)
    
    # ETH Trading Mode
    use_eth_trading: bool  # If True, trade native ETH instead of WETH
    eth_gas_reserve: float  # ETH amount to reserve for gas (default: 0.001)
    
    # Gas Settings
    gas_limit_multiplier: float  # Multiplier for gas limit (default: 1.05)
    gas_price_multiplier: float  # Multiplier for gas price (default: 1.05)
    
    # Gridless Cooldown
    gridless_buy_cooldown_seconds: int  # Seconds between gridless buys (default: 300)
    gridless_buy_execution_margin: float  # Allowed recovery toward buy threshold before execution
    
    # RPC Rotation (optional, defaults to None = single RPC_URL mode)
    rpc_urls: Optional[list] = None  # List of RPC URLs for rotation/failover
    
    # Dashboard Reporting (optional — bot works fine without these)
    dashboard_url: str = ""       # e.g. http://localhost:3000
    dashboard_api_key: str = ""   # Shared X-API-Key for dashboard ingestion
    bot_id: str = "grid-bot-1"    # Unique identifier shown on dashboard
    dashboard_name: str = ""      # Optional display name
    dashboard_group: str = ""     # Optional dashboard grouping label
    
    # Derived properties
    @property
    def chain_name(self) -> str:
        """Get the human-readable chain name."""
        return CHAIN_CONFIG.get(self.chain_id, {}).get("name", "Unknown")
    
    @property
    def zero_x_api_url(self) -> str:
        """Get the 0x API URL for the configured chain."""
        return ZEROX_API_URLS.get(self.chain_id, "https://api.0x.org")
    
    def validate(self) -> None:
        """
        Validate the configuration settings.
        
        Raises:
            ValueError: If any required configuration is missing or invalid.
        """
        # Check required fields
        if not self.private_key or self.private_key == "0x...":
            raise ValueError("PRIVATE_KEY is required and must be set")
        
        # RPC_URL is required only if RPC_URLS is not set
        rpc_urls = getattr(self, 'rpc_urls', None)
        if not rpc_urls:
            if not self.rpc_url or self.rpc_url == "https://...":
                raise ValueError("RPC_URL is required (or set RPC_URLS for rotation)")
        
        # Validate the resolved provider and its credentials. Explicit
        # SWAP_PROVIDER takes precedence; legacy flags remain compatible.
        provider = (self.swap_provider or "").strip().lower()
        provider = {"li.fi": "lifi", "li_fi": "lifi", "zero_x": "0x", "zerox": "0x", "sushi": "sushiswap"}.get(provider, provider)
        if not provider:
            provider = "uniswap" if self.use_uniswap_api else ("lifi" if self.use_li_fi else "0x")
        if provider not in {"0x", "lifi", "uniswap", "sushiswap"}:
            raise ValueError(f"Unsupported SWAP_PROVIDER: {provider}")
        if provider == "lifi":
            if not self.li_fi_api_key:
                raise ValueError("LI_FI_API_KEY is required for the lifi provider")
        elif provider == "uniswap":
            if not self.uniswap_api_key:
                raise ValueError("UNISWAP_API_KEY is required for the uniswap provider")
        elif provider == "0x":
            if not self.zero_x_api_key or self.zero_x_api_key == "...":
                raise ValueError("ZEROX_API_KEY is required for the 0x provider")
        
        if not self.token_address or self.token_address == "0x...":
            raise ValueError("TOKEN_ADDRESS is required and must be set")
        
        # Validate chain ID
        if self.chain_id not in CHAIN_CONFIG:
            raise ValueError(f"Unsupported chain ID: {self.chain_id}")
        
        # Validate numeric ranges
        if self.grid_spacing_percent <= 0:
            raise ValueError("GRID_SPACING_PERCENT must be positive")
        
        if self.max_positions <= 0:
            raise ValueError("MAX_POSITIONS must be positive")
        
        if not 0 <= self.bank_percentage <= 100:
            raise ValueError("BANK_PERCENTAGE must be between 0 and 100")
        
        if self.poll_interval_seconds < 1:
            raise ValueError("POLL_INTERVAL_SECONDS must be at least 1 second")

        if not 0 <= self.gridless_buy_execution_margin <= 100:
            raise ValueError("GRIDLESS_BUY_EXECUTION_MARGIN must be between 0 and 100")


def load_config(env_file: Optional[str] = None) -> BotConfig:
    """
    Load configuration from environment variables.
    
    Args:
        env_file: Optional path to a .env file to load.
        
    Returns:
        BotConfig: Validated configuration object.
        
    Raises:
        ValueError: If configuration validation fails.
    """
    # Load environment file if specified
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()
    
    # Get chain ID first to determine defaults
    chain_id = int(os.getenv("CHAIN_ID", "4663"))
    chain_defaults = CHAIN_CONFIG.get(chain_id, {})
    
    # Build configuration from environment
    config = BotConfig(
        # Wallet and Connection
        private_key=os.getenv("PRIVATE_KEY", ""),
        rpc_url=os.getenv("RPC_URL", ""),
        chain_id=chain_id,
        rpc_urls=_parse_rpc_urls(os.getenv("RPC_URLS", "")),
        
        # API Keys
        zero_x_api_key=os.getenv("ZEROX_API_KEY", ""),
        
        # Token Addresses
        token_address=os.getenv("TOKEN_ADDRESS", ""),
        token_symbol=os.getenv("TOKEN_SYMBOL", "TOKEN"),
        weth_address=os.getenv(
            "WETH_ADDRESS", 
            chain_defaults.get("weth", "")
        ),
        usdg_address=os.getenv("USDG_ADDRESS", ""),
        treasury_allowed_recipients=[
            address.strip()
            for address in os.getenv("TREASURY_ALLOWED_RECIPIENTS", "").split(",")
            if address.strip()
        ],
        permit2_address=chain_defaults.get("permit2", ""),
        zero_x_proxy=chain_defaults.get("zero_x_proxy", ""),
        uniswap_router=chain_defaults.get("uniswap_router", ""),
        
        # Trading Parameters
        grid_spacing_percent=float(os.getenv("GRID_SPACING_PERCENT", "5.0")),
        max_positions=int(os.getenv(
            "MAX_POSITIONS",
            str(chain_defaults.get("default_max_positions", 20))
        )),
        max_active_positions=int(os.getenv("MAX_ACTIVE_POSITIONS", os.getenv("MAX_POSITIONS", "10"))),
        min_profit_percent=float(os.getenv("MIN_PROFIT_PERCENT", "5.0")),
        initial_buy_amount=float(os.getenv("INITIAL_BUY_AMOUNT", "0.01")),
        slippage_tolerance=float(os.getenv("SLIPPAGE_TOLERANCE", "2.0")),
        bank_percentage=float(os.getenv("BANK_PERCENTAGE", "20")),
        moonbag_percentage=float(os.getenv("MOONBAG_PERCENTAGE", "1")),
        bank_min_amount=float(os.getenv("BANK_MIN_AMOUNT", "0.2")),
        fast_profit=os.getenv("FAST_PROFIT", "true").lower() == "true",
        tradeable_balance_percent=float(os.getenv("TRADEABLE_BALANCE_PERCENT", "100")),
        
        # Bot Behavior
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "6")),
        anti_mev_jitter=os.getenv("ANTI_MEV_JITTER", "true").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        state_file=os.getenv("STATE_FILE", "./data/positions.json"),
        compact_mode=os.getenv("COMPACT_MODE", "false").lower() == "true",
        minimal_logs=os.getenv("MINIMAL_LOGS", "false").lower() == "true",
        
        # API Provider Selection
        swap_provider=os.getenv("SWAP_PROVIDER", ""),
        sushi_api_key=os.getenv("SUSHI_API_KEY", ""),
        use_li_fi=os.getenv("USE_LI_FI", "false").lower() == "true",
        li_fi_api_key=os.getenv("LI_FI_API_KEY", ""),
        li_fi_integrator=os.getenv("LI_FI_INTEGRATOR", ""),
        
        use_uniswap_api=os.getenv("USE_UNISWAP_API", "true").lower() == "true",
        uniswap_api_key=os.getenv("UNISWAP_API_KEY", ""),
        uniswap_permit2_disabled=os.getenv("UNISWAP_PERMIT2_DISABLED", "true").lower() == "true",
        
        # Gridless Trading Mode
        use_gridless=os.getenv("USE_GRIDLESS", "true").lower() == "true",
        gridless_buy_threshold=float(os.getenv("GRIDLESS_BUY_THRESHOLD", "-10.0")),
        gridless_sell_threshold=float(os.getenv("GRIDLESS_SELL_THRESHOLD", "5.0")),
        gridless_stoploss_threshold=float(os.getenv("GRIDLESS_STOPLOSS_THRESHOLD", "-25.0")),
        gridless_stoploss_enabled=os.getenv("GRIDLESS_STOPLOSS_ENABLED", "false").lower() == "true",
        gridless_leading_edge=os.getenv("GRIDLESS_LEADING_EDGE", "true").lower() == "true",
        
        # ETH Trading Mode
        use_eth_trading=os.getenv("USE_ETH_TRADING", "false").lower() == "true",
        eth_gas_reserve=float(os.getenv("ETH_GAS_RESERVE", "0.001")),
        
        # Gas Settings
        gas_limit_multiplier=float(os.getenv("GAS_LIMIT_MULTIPLIER", "1.05")),
        gas_price_multiplier=float(os.getenv("GAS_PRICE_MULTIPLIER", "1.05")),
        
        # Gridless Cooldown
        gridless_buy_cooldown_seconds=int(os.getenv("GRIDLESS_BUY_COOLDOWN_SECONDS", "0")),
        gridless_buy_execution_margin=float(os.getenv("GRIDLESS_BUY_EXECUTION_MARGIN", "50")),
        
        # Dashboard Reporting
        dashboard_url=os.getenv("DASHBOARD_URL", ""),
        dashboard_api_key=os.getenv("DASHBOARD_API_KEY", ""),
        bot_id=os.getenv("BOT_ID", os.getenv("TOKEN_SYMBOL", f"grid-bot-{chain_id}")),
        dashboard_name=os.getenv("DASHBOARD_NAME", ""),
        dashboard_group=os.getenv("DASHBOARD_GROUP", ""),
    )
    
    # Validate the configuration
    config.validate()
    
    return config


# Global config instance (lazy loaded)
_config: Optional[BotConfig] = None


def get_config() -> BotConfig:
    """
    Get the global configuration instance.
    
    Returns:
        BotConfig: The cached configuration object.
    """
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(env_file: Optional[str] = None) -> BotConfig:
    """
    Reload the configuration from environment.
    
    Args:
        env_file: Optional path to a .env file to load.
        
    Returns:
        BotConfig: The newly loaded configuration.
    """
    global _config
    _config = load_config(env_file)
    return _config
