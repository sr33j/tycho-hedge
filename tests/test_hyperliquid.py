"""
Example usage of the HyperliquidClient
"""
import asyncio
import os
from dotenv import load_dotenv
from hyperliquid_client import HyperliquidClient

load_dotenv()

async def main():
    # Initialize client
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise ValueError("PRIVATE_KEY not found in environment variables")
    
    # Use testnet for safety
    client = HyperliquidClient()
    await client.initialize(private_key)
    
    print(f"Initialized client for address: {client.address}")
    
    # Get account value
    account_value = await client.get_account_value()
    print(f"Account value: ${account_value:.2f}")
    
    # Check ETH position
    eth_position = await client.get_position_size("ETH")
    print(f"ETH position size: {eth_position}")
    
    # Get ETH mark price
    eth_price = await client.get_mark_price("ETH")
    print(f"ETH mark price: ${eth_price:.2f}")
    
    # Get ETH funding rate
    eth_funding = await client.get_funding_rate("ETH")
    print(f"ETH funding rate: {eth_funding:.6f}")
    
    # Get funding history
    funding_history = await client.get_funding_history("ETH", days=1)
    if funding_history:
        avg_funding = sum(funding_history) / len(funding_history)
        print(f"Average funding rate (1 day): {avg_funding:.6f}")
        print(f"Annualized: {avg_funding * 365 * 3 * 100:.2f}%")

if __name__ == "__main__":
    asyncio.run(main())