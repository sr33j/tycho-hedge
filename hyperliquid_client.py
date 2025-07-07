from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account.signers.local import LocalAccount
from typing import Optional
from dotenv import load_dotenv
import os

load_dotenv()

PUBLIC_ADDRESS = os.getenv("PUBLIC_ADDRESS")

class HyperliquidClient:
    def __init__(self):
        self.account: Optional[LocalAccount] = None
        self.exchange: Optional[Exchange] = None
        self.info: Optional[Info] = None
        self.address = PUBLIC_ADDRESS
        self.initialized = False
    
    async def initialize(self):
        """Initialize the Hyperliquid client with account and exchange."""
        pass

    async def get_account_value(self) -> float:
        """Get the total account value from Hyperliquid."""
        pass

    async def get_position_size(self, asset: str) -> float:
        """Get current position size for the specified asset."""
        pass

    async def get_mark_price(self, asset: str) -> float:
        """Get current mark price for the specified asset."""
        pass

    async def get_funding_rate(self, asset: str) -> float:
        """Get current funding rate for the specified asset."""
        pass

    async def get_funding_history(self, asset: str, days: int = 7) -> List[float]:
        """Get historical funding rates for the specified asset."""
        pass

    async def adjust_position(self, asset: str, target_size: float, max_retries: int = 3) -> bool:
        """Adjust position size to target with retry logic."""
        pass

    async def withdraw_to_arbitrum(self, amount: float, max_retries: int = 3) -> bool:
        """Withdraw USDC from Hyperliquid to Arbitrum with retry logic."""
        pass