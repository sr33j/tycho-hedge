from typing import Optional
from web3 import Web3
from dotenv import load_dotenv
import os

load_dotenv()

PUBLIC_ADDRESS = os.getenv("PUBLIC_ADDRESS")

class TychoClient:
    def __init__(self):
        self.w3: Optional[Web3] = None
        self.wallet_address = PUBLIC_ADDRESS
        self.initialized = False
        
        # ERC20 ABI
        self.erc20_abi = [
            {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], 
             "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "decimals", 
             "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
        ]

    async def initialize(self):
        """Initialize Web3 connection to Unichain."""
        pass

    async def get_token_balance(self, token_address: str) -> float:
        """Get ERC20 token balance for the wallet address."""
        pass

    async def swap_tokens(self, from_token: str, to_token: str, amount: float, max_retries: int = 3) -> bool:
        """Execute token swap via Tycho with retry logic."""
        pass
