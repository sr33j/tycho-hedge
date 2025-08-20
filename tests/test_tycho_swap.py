import asyncio
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tycho_client import TychoClient

async def main():
    # Token addresses on Unichain
    USDC = "0x078d782b760474a361dda0af3839290b0ef57ad6"  # USDC on Unichain
    WETH = "0x4200000000000000000000000000000000000006"  # WETH on Unichain
    
    client = TychoClient()
    await client.initialize()
    
    # Get balances
    usdc_balance = await client.get_token_balance(USDC)
    weth_balance = await client.get_token_balance(WETH)
    eth_balance = await client.get_token_balance("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")
    
    print(f"USDC balance: {usdc_balance}")
    print(f"WETH balance: {weth_balance}")
    print(f"ETH balance: {eth_balance}")
    
    # Swap 0.5 USDC for WETH (we only have 0.749231 USDC)
    amount_to_swap = 0.5
    print(f"\nSwapping {amount_to_swap} USDC for WETH...")
    
    success = await client.swap_tokens(USDC, WETH, amount_to_swap)
    print(success)
    print(f"Swap {'successful' if success else 'failed'}")
    
    # Check new balances
    if success:
        new_usdc_balance = await client.get_token_balance(USDC)
        new_weth_balance = await client.get_token_balance(WETH)
        print(f"\nNew USDC balance: {new_usdc_balance}")
        print(f"New WETH balance: {new_weth_balance}")

if __name__ == "__main__":
    asyncio.run(main())