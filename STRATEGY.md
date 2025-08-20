# Funding Rate Arbitrage Strategy Documentation

## Table of Contents
1. [Strategy Overview](#strategy-overview)
2. [How the Trade Makes Money](#how-the-trade-makes-money)
3. [Risks of the Trade](#risks-of-the-trade)
4. [Execution Steps](#execution-steps)
5. [Codebase Implementation](#codebase-implementation)

## Strategy Overview

This strategy implements a **delta-neutral funding rate arbitrage** between Hyperliquid perpetual futures and Unichain spot markets. The core principle is to maintain offsetting positions: a **3x leveraged short position** on Hyperliquid perpetuals balanced by **spot holdings** on Unichain. This setup allows the strategy to profit from funding rate differentials while remaining largely price-neutral.

### Key Components
- **Perpetual Position**: Short position on Hyperliquid (operates on Arbitrum)
- **Spot Position**: Long position on Unichain (ETH, BTC, or UNI)
- **Cross-Chain Bridge**: Across Protocol for USDC and ETH transfers
- **Automated Rebalancing**: Maintains target leverage through periodic adjustments

## How the Trade Makes Money

### Funding Rate Mechanics

1. **Perpetual Funding Rates**: 
   - Perpetual futures contracts require periodic funding payments between long and short positions
   - When funding is positive (typical in bull markets), longs pay shorts
   - The strategy collects these payments by maintaining a short position

2. **Profit Calculation**:
   ```
   Daily Profit = Funding Rate × Position Size × Price
   
   Example: 
   - Funding Rate: 0.01% per 8 hours (0.03% daily)
   - Position Size: -3 ETH (short)
   - ETH Price: $3,000
   - Daily Profit: 0.0003 × 3 × $3,000 = $2.70
   ```

3. **Delta Neutrality**:
   - The short perpetual position loses money when price rises
   - The spot position gains an equal amount when price rises
   - Net price exposure is zero (excluding leverage adjustments)

### Leverage Optimization

The strategy uses 3x leverage to maximize capital efficiency:
- **Capital Allocation**: For $10,000 total capital
  - Hyperliquid Collateral: $2,500 (supports $7,500 short position at 3x)
  - Unichain Spot: $7,500 (matches the short exposure)
- **Return Enhancement**: 3x leverage triples the funding rate returns while maintaining delta neutrality

## Risks of the Trade

### 1. Liquidation Risk
- **Cause**: Extreme price movements can trigger liquidation of the leveraged short
- **Mitigation**: 
  - Automated rebalancing maintains leverage within safe bounds (3x ± 0.5x)
  - Position monitoring every minute with alerts
  - Liquidation price tracking in `StrategyState`

### 2. Negative Funding Risk
- **Cause**: In bear markets, funding can turn negative (shorts pay longs)
- **Mitigation**:
  - 7-day funding rate analysis with statistical thresholds
  - Automatic position unwinding when `(avg_funding - std_funding) < 0`
  - Continuous monitoring via `check_funding_rate()`

### 3. Bridge Risk
- **Cause**: Cross-chain bridges can fail or be exploited
- **Mitigation**:
  - Uses established Across Protocol with proven security
  - Implements retry logic with exponential backoff
  - Split operations: Unichain → Arbitrum → Hyperliquid for better error handling

### 4. Slippage Risk
- **Cause**: Large trades can move markets, especially on Unichain
- **Mitigation**:
  - 5% slippage tolerance on Hyperliquid orders
  - Tycho simulation engine for optimal routing on Unichain
  - Minimum trade sizes to avoid excessive fees

### 5. Gas Cost Risk
- **Cause**: High network fees can erode profits
- **Mitigation**:
  - Automated gas management maintains minimum ETH balances
  - Cross-chain ETH balancing to optimize costs
  - Batched operations where possible

## Execution Steps

### 1. Initial Setup
```python
# Environment Configuration
- PRIVATE_KEY: Trading wallet private key
- PUBLIC_ADDRESS: Trading wallet address  
- TYCHO_API_KEY: For spot trading simulation
- RPC URLs: Arbitrum and Unichain endpoints
```

### 2. Position Establishment
```python
async def execute_strategy():
    # Step 1: Check funding profitability
    is_profitable = await check_funding_rate()  # 7-day analysis
    
    # Step 2: Calculate optimal allocation
    T = total_portfolio_value
    C = T / (TARGET_LEVERAGE + 1)  # Collateral for 3x leverage
    
    # Step 3: Open positions
    - Short C × TARGET_LEVERAGE worth on Hyperliquid
    - Buy equivalent spot on Unichain
```

### 3. Rebalancing Logic

The strategy uses a sophisticated 3-case rebalancing system:

**Case 1: Excess on Hyperliquid** (x ≥ C)
```python
if hyperliquid_value >= optimal_collateral:
    1. Bridge excess USDC to Unichain
    2. Swap USDC to spot asset
    3. Adjust perpetual position
```

**Case 2: Excess USDC on Unichain** (x + y ≥ C)
```python
elif hyperliquid_value + unichain_usdc >= optimal_collateral:
    1. Bridge USDC to Hyperliquid
    2. Swap remaining USDC to spot
    3. Adjust perpetual position
```

**Case 3: Excess Spot on Unichain** (x + y < C)
```python
else:
    1. Swap excess spot to USDC
    2. Bridge USDC to Hyperliquid  
    3. Adjust perpetual position
```

### 4. Position Monitoring
- **Frequency**: Every 60 seconds for position data, every 10 minutes for rebalancing
- **Data Storage**: JSON records in `strategy_state.csv`
- **Metrics Tracked**: Leverage, funding rate, liquidation price, balances across chains

### 5. Unwinding Positions
```bash
python main.py --unwind
```
- Closes all perpetual positions
- Converts all spot to USDC
- Consolidates funds for withdrawal

## Codebase Implementation

### Core Architecture

```
tycho-hedge/
├── main.py                 # Strategy orchestrator
├── hyperliquid_client.py   # Perpetual trading interface
├── tycho_client.py         # Spot trading via Rust engine
├── across_client.py        # Cross-chain bridging
├── gas_manager.py          # ETH balance management
└── dashboard.py            # Real-time monitoring
```

### Key Implementation Details

#### 1. Multi-Chain Balance Tracking
```python
# main.py:133-193
async def get_position_balances() -> StrategyState:
    # Parallel execution of balance queries
    balance_tasks = [
        perp_client.get_account_value(),           # Hyperliquid
        swap_client.get_token_balance(USDC),       # Unichain USDC
        swap_client.get_token_balance(ASSET),      # Unichain spot
        get_arbitrum_usdc_balance()                # Arbitrum USDC
    ]
    results = await asyncio.gather(*balance_tasks)
```

#### 2. Funding Rate Analysis
```python
# main.py:212-241
async def check_funding_rate() -> bool:
    funding_history = await perp_client.get_funding_history(ASSET, days=7)
    avg_funding = mean(funding_history)
    std_funding = stdev(funding_history)
    
    # Profitable if average minus one std dev is positive
    threshold = avg_funding - std_funding
    return threshold > 0
```

#### 3. Cross-Chain Operations
```python
# Split bridge operations for reliability
# Unichain → Hyperliquid (via Arbitrum)
async def bridge_from_unichain_to_hyperliquid(usdc_amount):
    # Step 1: Unichain → Arbitrum
    await bridge_client.bridge_usdc_unichain_to_arbitrum(usdc_amount)
    
    # Step 2: Arbitrum → Hyperliquid  
    await perp_client.deposit_to_hyperliquid(usdc_amount)
```

#### 4. Gas Management
```python
# gas_manager.py:29-191
async def check_and_refill_gas():
    # Check ETH on both chains
    if uni_eth < MIN_ETH or arb_eth < MIN_ETH:
        # Swap tokens to ETH if needed
        # Balance ETH between chains via WETH bridges
```

#### 5. Position Adjustment
```python
# hyperliquid_client.py:118-159
async def adjust_position(asset, target_size):
    current_size = await get_position_size(asset)
    size_diff = target_size - current_size
    
    # Market order with 5% slippage tolerance
    result = exchange.market_open(
        name=asset,
        is_buy=size_diff > 0,
        sz=abs(size_diff),
        slippage=0.05
    )
```

### Monitoring and Safety

#### Real-time Dashboard (`dashboard.py`)
- PnL tracking (absolute and percentage)
- AUM across all chains
- Liquidation price monitoring
- Historical performance charts
- Recent trade history

#### Automated Safety Features
1. **Continuous Monitoring**: Position checks every minute
2. **Leverage Guards**: Rebalancing when leverage exceeds 3.5x or drops below 2.5x
3. **Funding Analysis**: Statistical evaluation of 7-day funding rates
4. **Gas Management**: Automated ETH refills to prevent transaction failures
5. **Error Recovery**: Retry logic with exponential backoff for all operations

### Performance Optimization

1. **Parallel Execution**: All independent operations use `asyncio.gather()`
2. **Efficient Routing**: Tycho simulation engine finds optimal swap paths
3. **State Caching**: Minimizes redundant RPC calls
4. **Precision Handling**: `round_down_amount()` prevents rounding errors

This implementation provides a robust, automated system for capturing funding rate arbitrage opportunities while managing the complex challenges of cross-chain operations and leveraged positions.