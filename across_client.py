class AcrossClient:
    def __init__(self):
        self.initialized = False

    async def initialize(self):
        """Initialize bridge client connections."""
        pass

    async def bridge_usdc_hyperliquid_to_unichain(self, amount: float, max_retries: int = 3) -> bool:
        """Bridge USDC from Hyperliquid to Unichain via Arbitrum."""
        pass

    async def bridge_asset_unichain_to_hyperliquid(self, amount: float, max_retries: int = 3) -> bool:
        """Bridge asset from Unichain to Hyperliquid via Arbitrum."""
        pass