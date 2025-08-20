from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from eth_account import Account
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
import os
import time
import asyncio
from web3 import Web3
import json

load_dotenv()

# Hyperliquid bridge contract on Arbitrum
HYPERLIQUID_BRIDGE_ADDRESS = '0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7'
# USDC on Arbitrum
USDC_ARB_ADDRESS = '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'
# Minimum deposit amount (5 USDC)
MIN_DEPOSIT_AMOUNT = 5.0

class HyperliquidClient:
    def __init__(self):
        self.account: Optional[Account] = None
        self.exchange: Optional[Exchange] = None
        self.info: Optional[Info] = None
        self.address: Optional[str] = None
        self.initialized = False
        self.api_url = constants.MAINNET_API_URL
        self.w3_arb: Optional[Web3] = None
        self.usdc_contract = None
    
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
            
            # Initialize Web3 for Arbitrum
            self.w3_arb = Web3(Web3.HTTPProvider(os.getenv('ARBITRUM_RPC_URL')))
            
            # Initialize USDC contract
            with open('abis/ERC20.json', 'r') as f:
                erc20_abi = json.load(f)
            self.usdc_contract = self.w3_arb.eth.contract(
                address=Web3.to_checksum_address(USDC_ARB_ADDRESS),
                abi=erc20_abi
            )
            
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
    
    async def deposit_to_hyperliquid(self, amount: float, max_retries: int = 3) -> bool:
        """
        Deposit USDC from Arbitrum to Hyperliquid by transferring to the bridge contract.
        
        Args:
            amount: Amount of USDC to deposit (must be >= 5 USDC)
            max_retries: Number of retry attempts
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.initialized:
            raise Exception("Client not initialized")
        
        # Validate minimum deposit amount
        if amount < MIN_DEPOSIT_AMOUNT:
            raise ValueError(f"Deposit amount must be at least {MIN_DEPOSIT_AMOUNT} USDC, got {amount}")
        
        # Convert to USDC units (6 decimals)
        amount_units = int(amount * 10**6)
        
        # Check USDC balance first
        usdc_balance = self.usdc_contract.functions.balanceOf(self.address).call()
        if usdc_balance < amount_units:
            raise Exception(f"Insufficient USDC balance. Have: {usdc_balance / 10**6}, Need: {amount}")
        
        # Check ETH balance for gas
        eth_balance = self.w3_arb.eth.get_balance(self.address)
        estimated_gas_cost = 100000 * self.w3_arb.eth.gas_price  # Conservative estimate
        if eth_balance < estimated_gas_cost:
            raise Exception(f"Insufficient ETH for gas. Have: {self.w3_arb.from_wei(eth_balance, 'ether')}, Need: {self.w3_arb.from_wei(estimated_gas_cost, 'ether')}")
        
        for attempt in range(max_retries):
            try:
                # Get current nonce
                nonce = self.w3_arb.eth.get_transaction_count(self.address)
                
                # Build transfer transaction
                tx = self.usdc_contract.functions.transfer(
                    Web3.to_checksum_address(HYPERLIQUID_BRIDGE_ADDRESS),
                    amount_units
                ).build_transaction({
                    'from': self.address,
                    'nonce': nonce,
                    'gasPrice': self.w3_arb.eth.gas_price,
                })
                
                # Estimate gas dynamically
                try:
                    estimated_gas = self.w3_arb.eth.estimate_gas(tx)
                    tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
                except Exception as gas_error:
                    print(f"Gas estimation failed, using fallback: {gas_error}")
                    tx['gas'] = 100000  # Fallback
                
                # Sign transaction
                signed_tx = self.w3_arb.eth.account.sign_transaction(tx, os.getenv("PRIVATE_KEY"))
                
                # Send transaction
                tx_hash = self.w3_arb.eth.send_raw_transaction(signed_tx.raw_transaction)
                
                print(f"Hyperliquid deposit transaction sent: {tx_hash.hex()}, amount: {amount} USDC")
                
                # Wait for confirmation
                receipt = self.w3_arb.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                
                if receipt['status'] == 1:
                    print(f"✅ Hyperliquid deposit transaction confirmed: {tx_hash.hex()}")
                    # Wait for the deposit to be credited on Hyperliquid (usually < 1 minute)
                    await asyncio.sleep(60)
                    return True
                else:
                    raise Exception(f"Transaction failed with status: {receipt['status']}, tx: {tx_hash.hex()}")
                    
            except Exception as e:
                print(f"❌ Deposit attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise Exception(f"Failed to deposit after {max_retries} attempts: {e}")
                await asyncio.sleep(5 * (attempt + 1))
        
        return False