import asyncio
import logging
import csv
import json
import argparse
from decimal import Decimal
from typing import Dict, Tuple, Optional, List, Any
from datetime import datetime, timedelta
from statistics import mean, stdev
from dataclasses import dataclass, asdict
from dotenv import load_dotenv
import os

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
import eth_account
from eth_account.signers.local import LocalAccount
from web3 import Web3
import structlog

from hyperliquid_client import HyperliquidClient
from tycho_client import TychoClient
from across_client import AcrossClient
from gas_manager import GasManager

load_dotenv()

logger = structlog.get_logger()

## PARAMETER DEFINITIONS (now loaded from environment variables)
OUTPUT_FILE = os.getenv("OUTPUT_FILE", "strategy_state.csv")
TARGET_LEVERAGE = float(os.getenv("TARGET_LEVERAGE", "3"))
LEVERAGE_BUFFER = float(os.getenv("LEVERAGE_BUFFER", "0.5"))
ASSET = os.getenv("ASSET", "ETH")
REBALANCE_SCHEDULE = int(os.getenv("REBALANCE_SCHEDULE", "600"))  # 10 minutes
POSITION_CHECK_SCHEDULE = int(os.getenv("POSITION_CHECK_SCHEDULE", "60"))  # 1 minute
FUNDING_RATE_LOOKBACK_PERIOD = int(os.getenv("FUNDING_RATE_LOOKBACK_PERIOD", "604800"))  # 7 days
MIN_BRIDGE_AMOUNT = float(os.getenv("MIN_BRIDGE_AMOUNT", "1"))
MIN_SWAP_AMOUNT = float(os.getenv("MIN_SWAP_AMOUNT", "1"))

# Gas management parameters
MIN_ETH_BALANCE = float(os.getenv("MIN_ETH_BALANCE", "0.0025"))  # Trigger threshold for gas refill
TARGET_ETH_BALANCE = float(os.getenv("TARGET_ETH_BALANCE", "0.005"))  # Target ETH balance after refill
ETH_ADDRESS = '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'  # Native ETH identifier
WETH_ADDRESS = '0x4200000000000000000000000000000000000006'  # WETH on Unichain

ASSET_TO_ADDRESS_MAP = {
    'ETH': '0x4200000000000000000000000000000000000006',
    'BTC': '0x0555E30da8f98308EdB960aa94C0Db47230d2B9c',
    'UNI': '0x8f187aA05619a017077f5308904739877ce9eA21',
    'USDC': '0x078d782b760474a361dda0af3839290b0ef57ad6',
}

## ENV VARS
PK = os.getenv("PRIVATE_KEY")
PUBLIC_ADDRESS = os.getenv("PUBLIC_ADDRESS")
TYCHO_URL = os.getenv("TYCHO_URL")
TYCHO_API_KEY = os.getenv("TYCHO_API_KEY")

@dataclass
class StrategyState:
    hyperliquid_account_value: float
    hyperliquid_perp_position_size: float
    unichain_usdc_balance: float
    unichain_asset_balance: float
    arbitrum_usdc_balance: float
    hyperliquid_asset_price: float
    funding_rate: float = 0.0
    current_leverage: float = 0.0
    last_rebalance: Optional[str] = None
    metadata: Optional[Dict] = None

# Global clients
perp_client = HyperliquidClient()
swap_client = TychoClient()
bridge_client = AcrossClient()

# Web3 client for Arbitrum (for balance checking)
w3_arb = None

# Initialize gas manager
gas_manager = GasManager(
    tycho_client=swap_client,
    perp_client=perp_client,
    bridge_client=bridge_client,
    min_eth_balance=MIN_ETH_BALANCE,
    target_eth_balance=TARGET_ETH_BALANCE,
    eth_address=ETH_ADDRESS,
    weth_address=WETH_ADDRESS,
    asset_to_address_map=ASSET_TO_ADDRESS_MAP,
    asset=ASSET
)

def round_down_amount(amount: float, decimals: int = 6) -> float:
    """
    Round down amount to specified decimal places to avoid precision issues.
    Default to 6 decimals which works for both ETH and USDC swaps.
    """
    multiplier = 10 ** decimals
    return int(amount * multiplier) / multiplier

## STRATEGY IMPLEMENTATION

async def write_strategy_state(state: StrategyState):
    """Write the strategy state to a CSV file with JSON data."""
    try:
        state_dict = asdict(state)
        state_dict['timestamp'] = datetime.utcnow().isoformat()
        
        # Convert to JSON string for CSV storage
        json_data = json.dumps(state_dict)
        
        # Write to CSV
        file_exists = os.path.exists(OUTPUT_FILE)
        with open(OUTPUT_FILE, 'a', newline='') as csvfile:
            fieldnames = ['timestamp', 'json_data']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            if not file_exists:
                writer.writeheader()
            
            writer.writerow({
                'timestamp': state_dict['timestamp'],
                'json_data': json_data
            })
            
        logger.info("Strategy state written to CSV", file=OUTPUT_FILE)
    except Exception as e:
        logger.error("Failed to write strategy state", error=str(e))

async def get_position_balances() -> StrategyState:
    """
    Get current balances across all chains
    """
    try:
        # Initialize Web3 for Arbitrum if needed
        global w3_arb
        if w3_arb is None:
            w3_arb = Web3(Web3.HTTPProvider(os.getenv('ARBITRUM_RPC_URL')))
        
        # Get Arbitrum USDC balance
        usdc_arb_contract = w3_arb.eth.contract(
            address='0xaf88d065e77c8cC2239327C5EDb3A432268e5831',  # USDC on Arbitrum
            abi=json.load(open('abis/ERC20.json'))
        )
        
        # Execute all balance queries in parallel
        balance_tasks = [
            perp_client.get_account_value(),
            perp_client.get_position_size(ASSET),
            perp_client.get_mark_price(ASSET),
            perp_client.get_funding_rate(ASSET),
            swap_client.get_token_balance(ASSET_TO_ADDRESS_MAP['USDC']),
            swap_client.get_token_balance(ASSET_TO_ADDRESS_MAP[ASSET]),
            # Get Arbitrum USDC balance
            asyncio.to_thread(
                lambda: usdc_arb_contract.functions.balanceOf(os.getenv('PUBLIC_ADDRESS')).call() / 10**6
            )
        ]
        
        results = await asyncio.gather(*balance_tasks, return_exceptions=True)
        
        account_value = results[0] if not isinstance(results[0], Exception) else 0.0
        perp_size = results[1] if not isinstance(results[1], Exception) else 0.0
        price = results[2] if not isinstance(results[2], Exception) else 0.0
        funding_rate = results[3] if not isinstance(results[3], Exception) else 0.0
        usdc_balance = results[4] if not isinstance(results[4], Exception) else 0.0
        asset_balance = results[5] if not isinstance(results[5], Exception) else 0.0
        arbitrum_usdc_balance = results[6] if not isinstance(results[6], Exception) else 0.0
        
        # Calculate current leverage
        current_leverage = 0.0
        if account_value > 0 and price > 0:
            current_leverage = abs(perp_size) * price / account_value
        
        return StrategyState(
            hyperliquid_account_value=account_value,
            hyperliquid_perp_position_size=perp_size,
            unichain_usdc_balance=usdc_balance,
            unichain_asset_balance=asset_balance,
            arbitrum_usdc_balance=arbitrum_usdc_balance,
            hyperliquid_asset_price=price,
            funding_rate=funding_rate,
            current_leverage=current_leverage
        )
        
    except Exception as e:
        logger.error("Error getting position balances", error=str(e))
        # Return empty state on error
        return StrategyState(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

async def check_perp_leverage(asset_price: float, hyperliquid_account_value: float, hyperliquid_perp_position_size: float) -> bool:
    """
    Returns True if leverage is within acceptable range
    """
    if hyperliquid_account_value <= 0:
        return False
        
    current_leverage = abs(hyperliquid_perp_position_size) * asset_price / hyperliquid_account_value 
    
    within_range = (TARGET_LEVERAGE - LEVERAGE_BUFFER <= current_leverage <= TARGET_LEVERAGE + LEVERAGE_BUFFER)
    
    logger.info("Leverage check", 
                current=current_leverage, 
                target=TARGET_LEVERAGE, 
                within_range=within_range)
    
    return within_range

async def check_funding_rate() -> bool:
    """
    Check if funding rate strategy is profitable
    """
    try:
        funding_history = await perp_client.get_funding_history(ASSET, days=7)
        
        if len(funding_history) < 10:  # Need sufficient data
            logger.warning("Insufficient funding rate data")
            return True  # Default to profitable if no data
        
        avg_funding = mean(funding_history)
        std_funding = stdev(funding_history) if len(funding_history) > 1 else 0
        
        # Strategy is profitable if (avg - std) > 0
        threshold = avg_funding - std_funding
        is_profitable = threshold > 0
        
        logger.info("Funding rate analysis", 
                   avg=avg_funding, 
                   std=std_funding, 
                   threshold=threshold, 
                   profitable=is_profitable)
        
        return is_profitable
        
    except Exception as e:
        logger.error("Error checking funding rate", error=str(e))
        return True  # Default to profitable on error

async def adjust_perp_position(target_perp_size: float):
    """
    Adjust the perp position to the target size
    """
    try:
        success = await perp_client.adjust_position(ASSET, target_perp_size)
        if not success:
            logger.error("Failed to adjust perp position", target_size=target_perp_size)
            raise Exception("Position adjustment failed")
        
        logger.info("Perp position adjusted successfully", target_size=target_perp_size)
        
    except Exception as e:
        logger.error("Error adjusting perp position", error=str(e))
        raise

async def bridge_from_hyperliquid_to_unichain(usdc_amount: float):
    """
    Bridge USDC from Hyperliquid to Unichain
    """
    try:
        # First withdraw from Hyperliquid to Arbitrum
        withdraw_success = await perp_client.withdraw_to_arbitrum(usdc_amount)
        if not withdraw_success:
            raise Exception("Hyperliquid withdrawal failed")
        
        # Then bridge from Arbitrum to Unichain
        bridge_success = await bridge_client.bridge_usdc_arbitrum_to_unichain(usdc_amount)
        if not bridge_success:
            await bridge_client.close()
            raise Exception("Bridge to Unichain failed")
            
        logger.info("Successfully bridged USDC to Unichain", amount=usdc_amount)
        
    except Exception as e:
        await bridge_client.close()
        logger.error("Error bridging USDC to Unichain", error=str(e))
        raise

async def bridge_from_unichain_to_hyperliquid(usdc_amount: float):
    """
    Bridge asset from Unichain to Hyperliquid via Arbitrum
    """
    try:
        # Step 1: Bridge USDC from Unichain to Arbitrum
        logger.info("Bridging USDC from Unichain to Arbitrum", amount=usdc_amount)
        bridge_success = await bridge_client.bridge_usdc_unichain_to_arbitrum(usdc_amount)
        if not bridge_success:
            raise Exception("Bridge from Unichain to Arbitrum failed")
        
        logger.info("Successfully bridged USDC to Arbitrum", amount=usdc_amount)
        
        # Wait for bridge to settle
        await asyncio.sleep(30)
        
        # Step 2: Deposit USDC from Arbitrum to Hyperliquid
        logger.info("Depositing USDC from Arbitrum to Hyperliquid", amount=usdc_amount)
        deposit_success = await perp_client.deposit_to_hyperliquid(usdc_amount)
        if not deposit_success:
            raise Exception("Deposit from Arbitrum to Hyperliquid failed")
            
        logger.info("Successfully deposited USDC to Hyperliquid", amount=usdc_amount)
        
    except Exception as e:
        logger.error("Error in bridge/deposit process", error=str(e))
        raise

async def swap_from_usdc_to_asset(usdc_amount: float):
    """
    Swap USDC to asset on Unichain using the tycho executor 
    """
    try:
        # Round down amount to avoid precision issues
        rounded_amount = round_down_amount(usdc_amount)
        
        if rounded_amount <= 0:
            logger.warning("Rounded amount is zero or negative, skipping swap", 
                          original=usdc_amount, rounded=rounded_amount)
            return
            
        success = await swap_client.swap_tokens(
            ASSET_TO_ADDRESS_MAP['USDC'], 
            ASSET_TO_ADDRESS_MAP[ASSET], 
            rounded_amount
        )
        if not success:
            raise Exception("USDC to asset swap failed")
            
        logger.info("Successfully swapped USDC to asset", 
                   original_amount=usdc_amount, rounded_amount=rounded_amount)
        
    except Exception as e:
        logger.error("Error swapping USDC to asset", error=str(e))
        raise

async def swap_from_asset_to_usdc(asset_amount: float):
    """
    Swap asset to USDC on Unichain using the tycho executor 
    """
    try:
        # Round down amount to avoid precision issues
        rounded_amount = round_down_amount(asset_amount)
        
        if rounded_amount <= 0:
            logger.warning("Rounded amount is zero or negative, skipping swap", 
                          original=asset_amount, rounded=rounded_amount)
            return
            
        success = await swap_client.swap_tokens(
            ASSET_TO_ADDRESS_MAP[ASSET], 
            ASSET_TO_ADDRESS_MAP['USDC'], 
            rounded_amount
        )
        if not success:
            raise Exception("Asset to USDC swap failed")
            
        logger.info("Successfully swapped asset to USDC", 
                   original_amount=asset_amount, rounded_amount=rounded_amount)
        
    except Exception as e:
        logger.error("Error swapping asset to USDC", error=str(e))
        raise

async def rebalance(hyperliquid_account_value: float, unichain_usdc_balance: float, unichain_asset_balance: float, arbitrum_usdc_balance: float, asset_price: float):
    """
    Execute optimal cross-chain rebalancing based on 3-case logic.
    Bridging operations are executed first and must complete successfully before swapping.
    """
    try:
        # First, handle any USDC on Arbitrum
        if arbitrum_usdc_balance >= 5.0:  # Minimum deposit amount for Hyperliquid
            logger.info("Found USDC on Arbitrum, processing", amount=arbitrum_usdc_balance)
            
            # Calculate how much we need on Hyperliquid
            current_total_value = hyperliquid_account_value + unichain_usdc_balance + unichain_asset_balance * asset_price + arbitrum_usdc_balance
            optimal_collateral = current_total_value / (TARGET_LEVERAGE + 1)
            hyperliquid_needed = optimal_collateral - hyperliquid_account_value
            
            if hyperliquid_needed > 0:
                # Deposit to Hyperliquid (respecting minimum)
                deposit_amount = min(hyperliquid_needed, arbitrum_usdc_balance)
                if deposit_amount >= 5.0:
                    logger.info("Depositing USDC from Arbitrum to Hyperliquid", amount=deposit_amount)
                    success = await perp_client.deposit_to_hyperliquid(deposit_amount)
                    if not success:
                        logger.error("Failed to deposit USDC to Hyperliquid")
                        raise Exception("Deposit to Hyperliquid failed")
                    arbitrum_usdc_balance -= deposit_amount
            
            # Bridge any remaining USDC back to Unichain (only if above minimum bridge amount)
            if arbitrum_usdc_balance > MIN_BRIDGE_AMOUNT:
                logger.info("Bridging remaining USDC from Arbitrum to Unichain", amount=arbitrum_usdc_balance)
                success = await bridge_client.bridge_usdc_arbitrum_to_unichain(arbitrum_usdc_balance)
                if not success:
                    logger.error("Failed to bridge USDC to Unichain")
                    raise Exception("Bridge to Unichain failed")
        
        # Get updated balances after handling Arbitrum USDC
        updated_state = await get_position_balances()
        hyperliquid_account_value = updated_state.hyperliquid_account_value
        unichain_usdc_balance = updated_state.unichain_usdc_balance
        unichain_asset_balance = updated_state.unichain_asset_balance
        
        # Calculate optimal allocation
        T = hyperliquid_account_value + unichain_usdc_balance + unichain_asset_balance * asset_price
        C = T / (TARGET_LEVERAGE + 1)
        
        x = hyperliquid_account_value
        y = unichain_usdc_balance
        z = unichain_asset_balance
        p = asset_price
        
        target_perp_size = -TARGET_LEVERAGE * C / p
        
        logger.info("Rebalancing analysis", 
                   total_value=T, 
                   optimal_collateral=C, 
                   case_bounds=[x, y, z, p])
        
        # CASE 1: Excess collateral on Hyperliquid
        if x >= C:
            excess = x - C
            if excess > MIN_BRIDGE_AMOUNT:
                logger.info("Bridging excess collateral to Unichain", amount=excess)
                # step 1: bridge excess collateral to Unichain
                await bridge_from_hyperliquid_to_unichain(excess)
            updated_state = await get_position_balances()
            y = updated_state.unichain_usdc_balance
            if y > MIN_SWAP_AMOUNT:
                # step 2: swap asset to USDC on Unichain
                logger.info("Swapping asset to USDC on Unichain", amount=y)
                await swap_from_usdc_to_asset(y)
        ## CASE 2: Excess USDC on Unichain
        elif x+y >= C:
            excess = C - x
            if excess > MIN_BRIDGE_AMOUNT:
                logger.info("Bridging USDC to Hyperliquid", amount=excess)
                # step 1: bridge USDC to Hyperliquid
                await bridge_from_unichain_to_hyperliquid(excess)
            updated_state = await get_position_balances()
            y = updated_state.unichain_usdc_balance
            if y > MIN_SWAP_AMOUNT:
                # step 2: swap USDC to asset on Unichain
                logger.info("Swapping USDC to asset on Unichain", amount=excess)
                await swap_from_usdc_to_asset(y)
        ## CASE 3: Excess asset on Unichain
        else:
            excess_usd = C - x - y
            excess_asset = excess_usd / p
            if excess_usd > MIN_SWAP_AMOUNT:
                # step 1: swap asset to USDC on Unichain
                logger.info("Swapping asset to USDC on Unichain", amount=excess_asset)
                await swap_from_asset_to_usdc(excess_asset)
            updated_state = await get_position_balances()
            y = updated_state.unichain_usdc_balance
            if y > MIN_BRIDGE_AMOUNT:
                # step 2: bridge USDC to Hyperliquid
                logger.info("Bridging USDC to Hyperliquid", amount=y)
                await bridge_from_unichain_to_hyperliquid(y)

        # Get updated balances after bridging and swapping
        updated_state = await get_position_balances()
        x = updated_state.hyperliquid_account_value
        z = updated_state.unichain_asset_balance

        target_perp_size = -1*min((TARGET_LEVERAGE + LEVERAGE_BUFFER) * x / p, z)
        # Step 3: Adjust perp position to target
        await adjust_perp_position(target_perp_size)
        
        logger.info("Rebalancing completed successfully")
        
    except Exception as e:
        logger.error("Error during rebalancing", error=str(e))
        raise

async def unwind_trade():
    """
    Close all positions on Hyperliquid and swap all assets to USDC on Unichain
    """
    try:
        logger.info("Starting trade unwind")
        
        # Get current state
        state = await get_position_balances()
        await write_strategy_state(state)
        
        # Execute unwind operations in parallel
        unwind_tasks = [
            adjust_perp_position(0),  # Close all perp positions
            swap_from_asset_to_usdc(state.unichain_asset_balance) if state.unichain_asset_balance > 0 else None
        ]
        
        # Filter out None tasks
        unwind_tasks = [task for task in unwind_tasks if task is not None]
        
        if unwind_tasks:
            await asyncio.gather(*unwind_tasks, return_exceptions=True)
        
        # Get final state
        final_state = await get_position_balances()
        final_state.metadata = {'post_unwind': True}
        await write_strategy_state(final_state)
        
        logger.info("Trade unwind completed")
        
    except Exception as e:
        logger.error("Error during trade unwind", error=str(e))
        raise

async def execute_strategy():
    """
    Execute the main strategy logic
    """
    try:
        # Initialize all clients in parallel
        init_tasks = [
            perp_client.initialize(),
            swap_client.initialize(),
            bridge_client.initialize()
        ]
        await asyncio.gather(*init_tasks, return_exceptions=True)
        
        # Get current state
        state = await get_position_balances()
        await write_strategy_state(state)
        
        # Check if strategy should continue
        profitability_task = check_funding_rate()
        leverage_task = check_perp_leverage(
            state.hyperliquid_asset_price, 
            state.hyperliquid_account_value, 
            state.hyperliquid_perp_position_size
        )
        
        is_profitable, leverage_ok = await asyncio.gather(profitability_task, leverage_task)
            # Check and refill gas if needed (FIRST OPERATION)
        logger.info("Checking gas levels before rebalance")
        gas_ok = await gas_manager.check_and_refill_gas()
        if not gas_ok:
            logger.warning("Gas refill failed, continuing with rebalance anyway")

        if not is_profitable:
            logger.info("Funding rate not profitable, unwinding trade")
            await unwind_trade()
            return
        
        if not leverage_ok:
            logger.info("Leverage outside target range, rebalancing")
            await rebalance(
                state.hyperliquid_account_value,
                state.unichain_usdc_balance,
                state.unichain_asset_balance,
                state.arbitrum_usdc_balance,
                state.hyperliquid_asset_price
            )
            
            # Get updated state after rebalancing
            updated_state = await get_position_balances()
            updated_state.metadata = {'post_rebalance': True}
            await write_strategy_state(updated_state)
        
        logger.info("Strategy execution completed successfully")
        
    except Exception as e:
        logger.error("Error executing strategy", error=str(e))
        # In production, could implement alerting here
        raise

async def monitor_positions():
    """
    Position monitoring loop that writes position data frequently
    """
    logger.info("Starting position monitoring loop", 
               position_check_interval=POSITION_CHECK_SCHEDULE)
    
    while True:
        try:
            # Initialize clients if needed (lightweight check)
            if not hasattr(perp_client, '_initialized'):
                await perp_client.initialize()
            if not hasattr(swap_client, '_initialized'):
                await swap_client.initialize()
            
            # Get current position state and write to file
            state = await get_position_balances()
            state.metadata = {'monitoring_only': True}
            await write_strategy_state(state)
            
            logger.debug("Position data written", 
                        leverage=state.current_leverage,
                        perp_size=state.hyperliquid_perp_position_size,
                        account_value=state.hyperliquid_account_value)
            
        except Exception as e:
            logger.error("Position monitoring failed", error=str(e))
            # Continue monitoring despite errors
            
        await asyncio.sleep(POSITION_CHECK_SCHEDULE)

async def strategy_rebalance_loop():
    """
    Main strategy rebalance loop
    """
    logger.info("Starting strategy rebalance loop", 
               rebalance_interval=REBALANCE_SCHEDULE)
    
    while True:
        try:
            await execute_strategy()
            
        except Exception as e:
            logger.error("Strategy execution failed", error=str(e))
            await bridge_client.close()
            # Continue running despite errors
            await asyncio.sleep(60)  # Wait 1 minute before retry
            
        await asyncio.sleep(REBALANCE_SCHEDULE)

async def main():
    """
    Main function that runs both position monitoring and strategy rebalancing concurrently
    """
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Funding rate strategy')
    parser.add_argument('--unwind', action='store_true', 
                       help='Unwind all positions and exit')
    args = parser.parse_args()
    
    if args.unwind:
        logger.info("Unwind mode activated - closing all positions")
        
        # Initialize clients
        init_tasks = [
            perp_client.initialize(),
            swap_client.initialize(),
            bridge_client.initialize()
        ]
        await asyncio.gather(*init_tasks, return_exceptions=True)
        
        # Execute unwind and exit
        await unwind_trade()
        logger.info("Unwind completed, exiting")
        return
    
    logger.info("Starting funding rate strategy with dual loops", 
               asset=ASSET, 
               target_leverage=TARGET_LEVERAGE,
               rebalance_interval=REBALANCE_SCHEDULE,
               position_check_interval=POSITION_CHECK_SCHEDULE)
    
    # Run both loops concurrently
    await asyncio.gather(
        monitor_positions(),
        strategy_rebalance_loop(),
        return_exceptions=True
    )

if __name__ == "__main__":
    asyncio.run(main())