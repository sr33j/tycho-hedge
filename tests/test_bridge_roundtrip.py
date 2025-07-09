import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from across_client import AcrossClient
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

async def main():
    # Initialize client
    client = AcrossClient()
    await client.initialize()
    
    # Test amount: 1 USDC
    test_amount = 1
    
    # Get initial balances
    arb_balance_start = client.erc20_arb.functions.balanceOf(client.account).call() / 10**6
    uni_balance_start = client.erc20_uni.functions.balanceOf(client.account).call() / 10**6
    
    print(f"Initial balances:")
    print(f"  Arbitrum: {arb_balance_start:.6f} USDC")
    print(f"  Unichain: {uni_balance_start:.6f} USDC")
    
    # Bridge Arbitrum -> Unichain
    print(f"\nBridging {test_amount} USDC from Arbitrum to Unichain...")
    success = await client.bridge_usdc_arbitrum_to_unichain(test_amount)
    if not success:
        print("Bridge failed!")
        await client.close()
        return
    
    # Check balances after first bridge
    arb_balance_mid = client.erc20_arb.functions.balanceOf(client.account).call() / 10**6
    uni_balance_mid = client.erc20_uni.functions.balanceOf(client.account).call() / 10**6
    
    print(f"\nBalances after Arbitrum -> Unichain:")
    print(f"  Arbitrum: {arb_balance_mid:.6f} USDC (diff: {arb_balance_mid - arb_balance_start:.6f})")
    print(f"  Unichain: {uni_balance_mid:.6f} USDC (diff: {uni_balance_mid - uni_balance_start:.6f})")
    
    # Bridge back: Unichain -> Arbitrum
    print(f"\nBridging {test_amount} USDC from Unichain to Arbitrum...")
    success = await client.bridge_usdc_unichain_to_arbitrum(test_amount)
    if not success:
        print("Bridge back failed!")
        await client.close()
        return
        
    # Final balances
    arb_balance_end = client.erc20_arb.functions.balanceOf(client.account).call() / 10**6
    uni_balance_end = client.erc20_uni.functions.balanceOf(client.account).call() / 10**6
    
    print(f"\nFinal balances:")
    print(f"  Arbitrum: {arb_balance_end:.6f} USDC (diff from start: {arb_balance_end - arb_balance_start:.6f})")
    print(f"  Unichain: {uni_balance_end:.6f} USDC (diff from start: {uni_balance_end - uni_balance_start:.6f})")
    
    # Cleanup
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())