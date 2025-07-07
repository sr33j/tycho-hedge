from decimal import Decimal
import logging
from typing import Dict, Tuple
from dotenv import load_dotenv
import os
import asyncio

load_dotenv()

logger = logging.getLogger(__name__)

## PARAMETER DEFINTIONS

OUTPUT_FILE = "strategy_state.json"
TARGET_LEVERAGE = 3
LEVERAGE_BUFFER = .5
ASSET = 'ETH'
REBALANCE_SCHEDULE = 10 * 60 # 10 minutes
FUNDING_RATE_LOOKBACK_PERIOD = 7 * 24 * 60 * 60 # 7 days

ASSET_TO_ADDRESS_MAP = {
    'ETH': '0x4200000000000000000000000000000000000006',
}


## ENV VARS
PK = os.getenv("PK")
PUBLIC_ADDRESS = os.getenv("PUBLIC_ADDRESS")

## STRATEGY
async def write_strategy_state(state: Dict[str, float]):
    """
    Write the strategy state to a file
    """
    pass

async def get_position_balances() -> Dict[str, float]:
    """
    Returns:
    {
        'hyperliquid_account_value': X,
        'hyperliquid_perp_position_size': Y,
        'unichain_usdc_balance': Z,
        'unichain_asset_balance': W,
        'hyperliquid_asset_price': P,
    }
    """
    pass

async def check_perp_leverage(asset_price: float, hyperliquid_account_value: float, hyperliquid_perp_position_size: float) -> bool:
    """
    Returns True if leverage is within acceptable range
    """
    current_leverage = hyperliquid_perp_position_size * asset_price / hyperliquid_account_value 
    if TARGET_LEVERAGE - LEVERAGE_BUFFER <= current_leverage <= TARGET_LEVERAGE + LEVERAGE_BUFFER:
        return True
    else:
        return False

async def check_funding_rate() -> bool:
    """
    Get the average and std dev funding rate over the last FUNDING_RATE_LOOKBACK_PERIOD 
    If the average funding rate is greater than the std dev, then strategy is profitable
    If the average funding rate is less than the std dev, then strategy is unprofitable
    """
    async def _get_historical_funding_stats() -> Tuple[float, float]:
        ## get the average and std dev of the funding rate over the last FUNDING_RATE_LOOKBACK_PERIOD 
        pass 

    avg_funding, std_dev_funding = _get_historical_funding_stats()
    if avg_funding - std_dev_funding > 0:
        return True
    else:
        return False

async def adjust_perp_position(target_perp_size: float):
    """
    Adjust the perp position to the target size
    If the target size is greater than the current position size, open more position
    If the target size is less than the current position size, close some/all of the position
    """
    pass


async def bridge_from_hyperliquid_to_unichain(usdc_amount: float):
    """
    Bridge USDC from Hyperliquid to Unichain
    """
    ## first withdraw from hyperliquid to arbitrum

    ## then bridge from arbitrum to unichain
    pass

async def bridge_from_unichain_to_hyperliquid(asset_amount: float):
    """
    Bridge asset from Unichain to Hyperliquid
    """
    ## first bridge from unichain to arbitrum

    ## then deposit to hyperliquid from arbitrum
    pass

async def swap_from_usdc_to_asset(usdc_amount: float):
    """
    Swap USDC to asset on Unichain using the tycho executor 
    """
    pass


async def swap_from_asset_to_usdc(asset_amount: float):
    """
    Swap asset to USDC on Unichain using the tycho executor 
    """
    pass

async def rebalance(hyperliquid_account_value: float, unichain_usdc_balance: float, unichain_asset_balance: float, asset_price: float):
    """
    Execute optimal cross-chain rebalancing based on 3-case logic.
    
    Case 1: C > x + y (need more collateral) - bridge assets from Unichain to Hyperliquid
    Case 2: C < x (have excess collateral) - bridge collateral from Hyperliquid to Unichain  
    Case 3: x < C < x + y (just right) - swap between USDC and asset on Unichain
    
    Args:
        allocation: Dict containing optimal allocation values
    """
    async def _get_optimal_allocation() -> Tuple[float, float]:
        T = hyperliquid_account_value + unichain_usdc_balance + unichain_asset_balance * asset_price
        C = T / (TARGET_LEVERAGE + 1)
        return C, T
    
    C, T = await _get_optimal_allocation()
    x = hyperliquid_account_value
    y = unichain_usdc_balance
    z = unichain_asset_balance
    p = asset_price
    target_perp_size = -TARGET_LEVERAGE * C / p
    target_spot_size = TARGET_LEVERAGE * C / p
    
    async def _execute_case1_rebalancing() -> bool:
        """
        Case 1: C > x + y - Need more collateral
        Bridge assets from Unichain to Hyperliquid and adjust positions
        """
        deficit = C - x - y  # How much more collateral we need
        
        # Check if we have enough assets on Unichain to bridge
        available_asset_value = z * p
        
        if available_asset_value < deficit:
            logger.warning("Insufficient assets on Unichain for required collateral",
                            deficit=float(deficit),
                            available_value=float(available_asset_value))
            # Bridge what we can
            bridge_amount = z
        else:
            # Bridge exactly what we need
            bridge_amount = deficit / p
        
        # Bridge assets from Unichain to Hyperliquid
        if bridge_amount > 0:
            asset_symbol = "WETH" if ASSET == "ETH" else ASSET
            bridge_tx = await bridge_from_unichain_to_hyperliquid(bridge_amount)
                
                
    
    async def _execute_case2_rebalancing() -> bool:
        """
        Case 2: C < x - Have excess collateral
        Bridge collateral from Hyperliquid to Unichain
        """
        excess = x - C  # How much excess collateral we have
        
        # Bridge excess USDC from Hyperliquid to Unichain
        bridge_tx = await bridge_from_hyperliquid_to_unichain(excess)
        
    
    async def _execute_case3_rebalancing() -> bool:
        """
        Case 3: x < C < x + y - Just right collateral distribution
        Swap between USDC and asset on Unichain to achieve target positions
        """
        # Calculate target spot position value
        target_spot_value = target_spot_size * p
        current_spot_value = z * p
        
        # Determine if we need to buy or sell spot
        spot_delta_value = target_spot_value - current_spot_value
        
        if abs(spot_delta_value) > Decimal("1"):  # Min threshold
            if spot_delta_value > 0:
                # Need to buy more spot (sell USDC for asset)
                logger.info("Buying more spot on Unichain", amount_usd=float(spot_delta_value))
                swap_tx = await swap_from_usdc_to_asset(float(spot_delta_value))
            else:
                # Need to sell spot (sell asset for USDC)
                logger.info("Selling spot on Unichain", amount_usd=float(abs(spot_delta_value)))
                swap_tx = await swap_from_asset_to_usdc(float(abs(spot_delta_value)))
            

    # Determine which case we're in
    if C > x + y:
        # Case 1: Need more collateral - bridge assets from Unichain to Hyperliquid
        logger.info("Case 1: Bridging assets from Unichain to Hyperliquid")
        success = await _execute_case1_rebalancing()
        
    elif C < x:
        # Case 2: Have excess collateral - bridge from Hyperliquid to Unichain
        logger.info("Case 2: Bridging collateral from Hyperliquid to Unichain")
        success = await _execute_case2_rebalancing()
        
    else:
        # Case 3: Just right - swap between USDC and asset on Unichain
        logger.info("Case 3: Swapping on Unichain to rebalance")
        success = await _execute_case3_rebalancing()
    
    return success

async def unwind_trade():
    """
    Close all positions on Hyperliquid
    Swap all assets to USDC on Unichain
    """

    ## get the current state of the strategy
    state = await get_position_balances()

    ## write the state to the output file
    await write_strategy_state(state)

    ## close all positions on Hyperliquid
    await adjust_perp_position(0)

    ## swap all assets to USDC on Unichain
    await swap_from_asset_to_usdc(state['unichain_asset_balance'])


async def execute_strategy():
    """
    Execute the strategy
    """
    state = await get_position_balances()
    await write_strategy_state(state)

    ## check if the strategy is profitable
    if not await check_funding_rate():
        logger.info("Funding rate is not profitable, unwinding trade")
        await unwind_trade()
        state = await get_position_balances()
        state['metadata'] = {
            'post_unwind': True        
        }
        await write_strategy_state(state)
        return 
    
    ## check if our leverage is within the target range
    if not await check_perp_leverage(state['asset_price'], state['hyperliquid_account_value'], state['hyperliquid_perp_position_size']):
        logger.info("Leverage is not within the target range, unwinding trade")
        await rebalance(state['hyperliquid_account_value'], state['unichain_usdc_balance'], state['unichain_asset_balance'], state['asset_price'])
        state = await get_position_balances()
        state['metadata'] = {
            'post_rebalance': True        
        }
        await write_strategy_state(state)
        return 

async def main():
    ## run the strategy every REBALANCE_SCHEDULE
    while True:
        await execute_strategy()
        await asyncio.sleep(REBALANCE_SCHEDULE)

if __name__ == "__main__":
    asyncio.run(main())