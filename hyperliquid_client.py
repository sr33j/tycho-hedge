from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
import os
import time
import asyncio

load_dotenv()

class HyperliquidClient:
    def __init__(self):
        self.account: Optional[Account] = None
        self.exchange: Optional[Exchange] = None
        self.info: Optional[Info] = None
        self.address: Optional[str] = None
        self.initialized = False
        self.api_url = constants.MAINNET_API_URL
    
    async def initialize(self):
        """Initialize the Hyperliquid client with account and exchange."""
        try:
            private_key = os.getenv("PRIVATE_KEY")
            if not private_key:
                raise ValueError("PRIVATE_KEY not found in environment variables")
            self.account = Account.from_key(private_key)
            self.address = self.account.address
            
            self.info = Info(self.api_url, skip_ws=True)
            self.exchange = Exchange(self.account, self.api_url)
            
            self.initialized = True
        except Exception as e:
            raise Exception(f"Failed to initialize Hyperliquid client: {e}")

    async def get_account_value(self) -> float:
        """Get the total account value from Hyperliquid."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        user_state = self.info.user_state(self.address.lower())
        print(user_state)
        return float(user_state.get("crossMarginSummary", {}).get("accountValue", 0))

    async def get_position_size(self, asset: str) -> float:
        """Get current position size for the specified asset."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        user_state = self.info.user_state(self.address)
        positions = user_state.get("assetPositions", [])
        
        for position in positions:
            pos_data = position.get("position", {})
            if pos_data.get("coin") == asset:
                return float(pos_data.get("szi", 0))
        
        return 0.0

    async def get_mark_price(self, asset: str) -> float:
        """Get current mark price for the specified asset."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        all_mids = self.info.all_mids()
        return float(all_mids.get(asset, 0))

    async def get_funding_rate(self, asset: str) -> float:
        """Get current funding rate for the specified asset."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        meta = self.info.meta()
        universe = meta.get("universe", [])
        
        for coin_info in universe:
            if coin_info.get("name") == asset:
                return float(coin_info.get("funding", 0))
        
        return 0.0

    async def get_funding_history(self, asset: str, days: int = 7) -> List[float]:
        """Get historical funding rates for the specified asset."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        end_time = int(time.time() * 1000)
        start_time = end_time - (days * 24 * 60 * 60 * 1000)
        
        funding_history = self.info.funding_history(asset, start_time, end_time)
        
        return [float(entry.get("fundingRate", 0)) for entry in funding_history]

    async def adjust_position(self, asset: str, target_size: float, max_retries: int = 3) -> bool:
        """Adjust position size to target with retry logic."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        ## round size to 4 decimal places
        target_size = round(target_size, 4)
        
        for attempt in range(max_retries):
            try:
                current_size = await self.get_position_size(asset)
                size_diff = target_size - current_size
                
                if abs(size_diff) < 0.0001:
                    return True
                
                is_buy = size_diff > 0
                order_size = abs(size_diff)
                
                # Place market order with 5% slippage tolerance
                result = self.exchange.market_open(
                    name=asset,
                    is_buy=is_buy, 
                    sz=order_size,
                    slippage=0.05
                )
                
                if result.get("status") == "ok":
                    await asyncio.sleep(1)
                    
                    # Verify position
                    new_size = await self.get_position_size(asset)
                    if abs(new_size - target_size) < 0.0001:
                        return True
                
            except Exception as e:
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to adjust position after {max_retries} attempts: {e}")
                await asyncio.sleep(2 ** attempt)
        
        return False

    async def withdraw_to_arbitrum(self, amount: float, max_retries: int = 3) -> bool:
        """Withdraw USDC from Hyperliquid to Arbitrum with retry logic."""
        if not self.initialized:
            raise Exception("Client not initialized")
        
        for attempt in range(max_retries):
            try:
                # Withdraw to the same address on Arbitrum
                result = self.exchange.withdraw_from_bridge(
                    amount=amount,
                    destination=self.address
                )
                
                if result.get("status") == "ok":
                    return True
                    
            except Exception as e:
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to withdraw after {max_retries} attempts: {e}")
                await asyncio.sleep(5 * (attempt + 1))
        
        return False