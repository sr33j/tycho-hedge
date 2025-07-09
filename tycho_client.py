from typing import Optional
from web3 import Web3
from dotenv import load_dotenv
import os
import asyncio
import json

load_dotenv()

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

PUBLIC_ADDRESS = os.getenv("PUBLIC_ADDRESS")

class TychoClient:
    def __init__(self):
        self.w3: Optional[Web3] = None
        self.wallet_address = PUBLIC_ADDRESS
        self.initialized = False
        
        # ERC20 ABI - use absolute path
        with open(os.path.join(SCRIPT_DIR, 'abis', 'ERC20.json')) as f:
            self.erc20_abi = json.load(f)

    async def initialize(self):
        """Initialize Web3 connection to Unichain."""
        rpc_url = os.getenv("UNICHAIN_RPC_URL")
        if not rpc_url:
            raise ValueError("UNICHAIN_RPC_URL not set")
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError("Failed to connect to Unichain")
        self.initialized = True

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

    async def swap_tokens(self, from_token: str, to_token: str, amount: float, max_retries: int = 3) -> bool:
        """Execute token swap via Tycho Rust implementation."""
        if not self.initialized:
            await self.initialize()
        
        # Validate required environment variables
        required_env_vars = ["TYCHO_URL", "TYCHO_API_KEY", "UNICHAIN_RPC_URL", "PRIVATE_KEY"]
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
        
        # Get chain from environment or default to unichain
        chain = os.getenv("CHAIN", "unichain")
        
        # Build cargo run command arguments
        cmd_args = [
            "cargo", "run", "--release", "--example", "quickstart", "--",
            "--sell-token", from_token,
            "--buy-token", to_token,
            "--sell-amount", str(amount),
            "--chain", chain
        ]
        
        # Set environment variables that the Rust binary needs
        env = os.environ.copy()
        env.update({
            "TYCHO_URL": os.getenv("TYCHO_URL"),
            "TYCHO_API_KEY": os.getenv("TYCHO_API_KEY"),
            "RPC_URL": os.getenv("UNICHAIN_RPC_URL"),
            "PK": os.getenv("PRIVATE_KEY"),  # Note: Rust expects PK, not PRIVATE_KEY
            "RUST_LOG": "info",  # Enable logging
        })
        
        for attempt in range(max_retries):
            try:
                print(f"Executing swap attempt {attempt + 1}/{max_retries}...")
                print(f"Command: {' '.join(cmd_args)}")
                
                # Execute the Rust example
                process = await asyncio.create_subprocess_exec(
                    *cmd_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd='tycho-simulation/examples/quickstart'
                )
                
                stdout, stderr = await process.communicate()
                
                stdout_text = stdout.decode()
                stderr_text = stderr.decode()
                
                print("=== STDOUT ===")
                print(stdout_text)
                
                if stderr_text:
                    print("=== STDERR ===")
                    print(stderr_text)
                
                if process.returncode == 0:
                    print("✅ Swap executed successfully!")
                    return True
                else:
                    print(f"❌ Swap failed (attempt {attempt + 1}): Return code {process.returncode}")
                    
                    if attempt == max_retries - 1:
                        raise RuntimeError(f"Swap failed after {max_retries} attempts. Last error: {stderr_text}")
                    
                    # Wait before retrying (exponential backoff)
                    await asyncio.sleep(2 ** attempt)
                    
            except Exception as e:
                print(f"❌ Error executing swap (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        
        return False