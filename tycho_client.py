from typing import Optional, Dict, Any
from web3 import Web3
from dotenv import load_dotenv
import os
import asyncio
import json
import subprocess
import aiohttp

load_dotenv()

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PUBLIC_ADDRESS = os.getenv("PUBLIC_ADDRESS")

class TychoClient:
    def __init__(self):
        self.w3: Optional[Web3] = None
        self.wallet_address = PUBLIC_ADDRESS
        self.initialized = False
        # Replace rust binary path with service URL
        self.service_url = os.getenv("TYCHO_SERVICE_URL", "http://localhost:3000")
        
        # ERC20 ABI - use absolute path
        with open(os.path.join(SCRIPT_DIR, 'abis', 'ERC20.json')) as f:
            self.erc20_abi = json.load(f)
        
        # WETH ABI - use absolute path
        with open(os.path.join(SCRIPT_DIR, 'abis', 'WETH.json')) as f:
            self.weth_abi = json.load(f)

    async def initialize(self):
        """Initialize Web3 connection to Unichain and check service health."""
        rpc_url = os.getenv("UNICHAIN_RPC_URL")
        if not rpc_url:
            raise ValueError("UNICHAIN_RPC_URL not set")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError("Failed to connect to Unichain")
        
        # Check if service is healthy
        await self._check_service_health()
        self.initialized = True

    async def _check_service_health(self):
        """Check if the Tycho service is healthy and indexing."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.service_url}/health") as response:
                    if response.status == 200:
                        health_data = await response.json()
                        print(f"✅ Tycho service is healthy. Indexed pools: {health_data['indexed_pools']}")
                        if health_data['indexed_pools'] == 0:
                            print("⚠️ Warning: No pools indexed yet. Service may still be starting up.")
                    else:
                        raise ConnectionError(f"Service health check failed with status {response.status}")
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Tycho service at {self.service_url}: {e}")

    async def get_token_balance(self, token_address: str) -> float:
        """Get ERC20 token balance for the wallet address."""
        if not self.initialized:
            await self.initialize()
        
        if token_address.lower() == "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee":
            # Native ETH balance
            balance_wei = self.w3.eth.get_balance(self.wallet_address)
            return float(self.w3.from_wei(balance_wei, 'ether'))
        
        # ERC20 token balance
        token_contract = self.w3.eth.contract(address=self.w3.to_checksum_address(token_address), abi=self.erc20_abi)
        balance = token_contract.functions.balanceOf(self.wallet_address).call()
        decimals = token_contract.functions.decimals().call()
        return float(balance) / (10 ** decimals)

    async def get_quote(self, from_token: str, to_token: str, amount: float) -> Dict[str, Any]:
        """Get a quote for swapping tokens."""
        if not self.initialized:
            await self.initialize()
        
        quote_request = {
            "sell_token": from_token,
            "buy_token": to_token,
            "sell_amount": amount
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.service_url}/quote",
                    json=quote_request,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error_text = await response.text()
                        raise RuntimeError(f"Quote request failed: {error_text}")
        except Exception as e:
            print(f"❌ Error getting quote: {e}")
            raise

    async def swap_tokens(self, from_token: str, to_token: str, amount: float, max_retries: int = 1) -> bool:
        """Execute token swap via HTTP call to Tycho service."""
        if not self.initialized:
            await self.initialize()
        
        # Validate required environment variables
        required_env_vars = ["UNICHAIN_RPC_URL", "PRIVATE_KEY"]
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
        
        execute_request = {
            "sell_token": from_token,
            "buy_token": to_token,
            "sell_amount": amount
        }
        
        for attempt in range(max_retries):
            try:
                print(f"Executing swap (attempt {attempt + 1}/{max_retries})...")
                print(f"Swapping {amount} {from_token} for {to_token}")
                
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.service_url}/execute",
                        json=execute_request,
                        headers={"Content-Type": "application/json"}
                    ) as response:
                        if response.status == 200:
                            result = await response.json()
                            if result["success"]:
                                print(f"✅ Swap executed successfully! TX: {result.get('transaction_hash', 'N/A')}")
                                return True
                            else:
                                print(f"❌ Swap failed: {result.get('error', 'Unknown error')}")
                                if attempt == max_retries - 1:
                                    raise RuntimeError(f"Swap failed: {result.get('error', 'Unknown error')}")
                        else:
                            error_text = await response.text()
                            print(f"❌ Swap request failed: {error_text}")
                            if attempt == max_retries - 1:
                                raise RuntimeError(f"Swap request failed: {error_text}")
                    
                    # Wait before retrying
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2)
                    
            except Exception as e:
                print(f"❌ Error executing swap (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2)
        
        return False
        
    async def wrap_eth(self, amount: float) -> bool:
        """
        Wrap native ETH to WETH.
        
        Args:
            amount: Amount of ETH to wrap (in ETH units)
            
        Returns:
            bool: True if wrap was successful, False otherwise
        """
        if not self.initialized:
            await self.initialize()
            
        try:
            # WETH contract address on Unichain
            weth_address = "0x4200000000000000000000000000000000000006"
            weth_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(weth_address), 
                abi=self.weth_abi
            )
            
            # Convert amount to Wei
            amount_wei = self.w3.to_wei(amount, 'ether')
            
            # Check ETH balance (including gas costs)
            eth_balance = self.w3.eth.get_balance(self.wallet_address)
            estimated_gas_cost = 50000 * self.w3.eth.gas_price  # Conservative estimate
            total_needed = amount_wei + estimated_gas_cost
            
            if eth_balance < total_needed:
                print(f"❌ Insufficient ETH balance. Have: {self.w3.from_wei(eth_balance, 'ether')}, Need: {self.w3.from_wei(total_needed, 'ether')}")
                return False
                
            # Get private key for signing
            private_key = os.getenv("PRIVATE_KEY")
            if not private_key:
                raise ValueError("PRIVATE_KEY not set")
            
            # Build the deposit transaction (wrapping ETH to WETH)
            tx = weth_contract.functions.deposit().build_transaction({
                'from': self.wallet_address,
                'value': amount_wei,
                'nonce': self.w3.eth.get_transaction_count(self.wallet_address),
                'gasPrice': self.w3.eth.gas_price,
                'chainId': self.w3.eth.chain_id
            })
            
            # Estimate gas dynamically
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx)
                tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
            except Exception as gas_error:
                print(f"⚠️ Gas estimation failed, using fallback: {gas_error}")
                tx['gas'] = 50000  # Fallback
            
            # Sign and send transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            print(f"🔄 Wrapping {amount} ETH to WETH. Transaction: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt.status == 1:
                print(f"✅ Successfully wrapped {amount} ETH to WETH. TX: {tx_hash.hex()}")
                return True
            else:
                print(f"❌ ETH wrap transaction failed. TX: {tx_hash.hex()}, Status: {receipt.status}")
                return False
                
        except Exception as e:
            print(f"❌ Error wrapping ETH: {e}")
            return False
    
    async def unwrap_weth(self, amount: float) -> bool:
        """
        Unwrap WETH to native ETH.
        
        Args:
            amount: Amount of WETH to unwrap (in ETH units)
            
        Returns:
            bool: True if unwrap was successful, False otherwise
        """
        if not self.initialized:
            await self.initialize()
            
        try:
            # WETH contract address on Unichain
            weth_address = "0x4200000000000000000000000000000000000006"
            weth_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(weth_address), 
                abi=self.weth_abi
            )
            
            # Convert amount to Wei
            amount_wei = self.w3.to_wei(amount, 'ether')
            
            # Check WETH balance first
            weth_balance = weth_contract.functions.balanceOf(self.wallet_address).call()
            if weth_balance < amount_wei:
                print(f"❌ Insufficient WETH balance. Have: {self.w3.from_wei(weth_balance, 'ether')}, Need: {amount}")
                return False
            
            # Check ETH balance for gas
            eth_balance = self.w3.eth.get_balance(self.wallet_address)
            estimated_gas_cost = 100000 * self.w3.eth.gas_price  # Conservative estimate
            if eth_balance < estimated_gas_cost:
                print(f"❌ Insufficient ETH for gas. Have: {self.w3.from_wei(eth_balance, 'ether')}, Need: {self.w3.from_wei(estimated_gas_cost, 'ether')}")
                return False
            
            # Get private key for signing
            private_key = os.getenv("PRIVATE_KEY")
            if not private_key:
                raise ValueError("PRIVATE_KEY not set")
            
            # Build the transaction
            tx = weth_contract.functions.withdraw(amount_wei).build_transaction({
                'from': self.wallet_address,
                'nonce': self.w3.eth.get_transaction_count(self.wallet_address),
                'gasPrice': self.w3.eth.gas_price,
                'chainId': self.w3.eth.chain_id
            })
            
            # Estimate gas dynamically
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx)
                tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
            except Exception as gas_error:
                print(f"⚠️ Gas estimation failed, using fallback: {gas_error}")
                tx['gas'] = 100000  # Fallback
            
            # Sign and send transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            print(f"🔄 Unwrapping {amount} WETH to ETH. Transaction: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt.status == 1:
                print(f"✅ Successfully unwrapped {amount} WETH to ETH. TX: {tx_hash.hex()}")
                return True
            else:
                print(f"❌ WETH unwrap transaction failed. TX: {tx_hash.hex()}, Status: {receipt.status}")
                return False
                
        except Exception as e:
            print(f"❌ Error unwrapping WETH: {e}")
            return False