# Strategy Parameters and Configuration Guide

## Table of Contents
1. [Environment Variables](#environment-variables)
2. [Strategy Parameters](#strategy-parameters)
3. [Parameter Effects and Tuning](#parameter-effects-and-tuning)
4. [Recommended Settings](#recommended-settings)

## Environment Variables

All environment variables should be set in a `.env` file in the project root. Copy `.env.example` to `.env` and configure:

### Core Authentication

#### `PRIVATE_KEY`
- **Description**: Private key of the trading wallet (without 0x prefix)
- **Format**: 64 character hexadecimal string
- **Security**: NEVER commit this to version control
- **Example**: `a1b2c3d4e5f6...` (64 chars)
- **Used by**: All modules for transaction signing

#### `PUBLIC_ADDRESS`
- **Description**: Public address of the trading wallet
- **Format**: 42 character Ethereum address with 0x prefix
- **Example**: `0x742d35Cc6634C0532925a3b844Bc9e7595f8f123`
- **Used by**: Balance queries and transaction origination

#### `PK` (Deprecated)
- **Note**: Some code references this instead of `PRIVATE_KEY`
- **Action**: Set same value as `PRIVATE_KEY` for compatibility

### API Configuration

#### `TYCHO_API_KEY`
- **Description**: Authentication key for Tycho Protocol's indexing service
- **Purpose**: Enables efficient spot trading simulation on Unichain
- **Format**: API key string provided by Propeller Heads
- **Example**: `sampletoken`
- **Required for**: Spot trading operations

#### `TYCHO_URL`
- **Description**: Tycho Protocol API endpoint
- **Default**: `tycho-unichain-beta.propellerheads.xyz`
- **Format**: Domain without https:// prefix
- **Networks**: Different endpoints for mainnet/testnet

### Network Configuration

#### `UNICHAIN_RPC_URL`
- **Description**: RPC endpoint for Unichain network
- **Default**: `https://unichain-rpc.publicnode.com`
- **Purpose**: Spot trading and balance queries
- **Alternatives**: Private node URLs for better performance

#### `ARBITRUM_RPC_URL`
- **Description**: RPC endpoint for Arbitrum One
- **Default**: `https://arb1.arbitrum.io/rpc`
- **Purpose**: Hyperliquid deposits/withdrawals, USDC bridging
- **Note**: Hyperliquid operates on Arbitrum

#### `CHAIN`
- **Description**: Chain identifier for Tycho operations
- **Default**: `unichain`
- **Options**: `unichain`, others as supported
- **Used by**: Tycho client for chain-specific configurations

### Optional Variables

#### `RPC_URL`
- **Description**: Generic RPC URL used by Rust components
- **Default**: Can be same as `UNICHAIN_RPC_URL`
- **Used by**: `tycho-simulation` Rust crate

## Strategy Parameters

All parameters are defined at the top of `main.py` (lines 31-47):

### Position Management

#### `TARGET_LEVERAGE` (Default: 3)
- **Description**: Target leverage ratio for the perpetual short position
- **Range**: 1-5 (higher = more risk/reward)
- **Effect**: 
  - Higher values increase funding rate returns
  - Higher values increase liquidation risk
  - Affects capital allocation between chains
- **Formula**: `Perp Position Size = Account Value × TARGET_LEVERAGE`

#### `LEVERAGE_BUFFER` (Default: 0.5)
- **Description**: Acceptable deviation from target leverage before rebalancing
- **Range**: 0.1-1.0
- **Effect**:
  - Lower values = more frequent rebalancing (higher gas costs)
  - Higher values = less frequent rebalancing (higher liquidation risk)
- **Rebalance Triggers**: When leverage < 2.5x or > 3.5x (with default settings)

#### `ASSET` (Default: 'ETH')
- **Description**: Trading pair asset (must match Hyperliquid ticker)
- **Options**: 'ETH', 'BTC', 'UNI'
- **Considerations**:
  - Must have corresponding address in `ASSET_TO_ADDRESS_MAP`
  - Affects liquidity and funding rates
  - ETH typically has deepest liquidity

### Timing Parameters

#### `REBALANCE_SCHEDULE` (Default: 600 seconds / 10 minutes)
- **Description**: Interval between rebalancing checks
- **Range**: 300-3600 seconds (5 mins - 1 hour)
- **Trade-offs**:
  - Shorter = Better leverage maintenance, higher gas costs
  - Longer = Lower costs, risk of leverage drift
- **Recommendation**: 10-30 minutes for volatile markets

#### `POSITION_CHECK_SCHEDULE` (Default: 60 seconds / 1 minute)
- **Description**: Interval for position monitoring and data logging
- **Range**: 30-300 seconds
- **Purpose**: 
  - Updates dashboard data
  - Logs position state to CSV
  - No trading actions taken
- **Note**: Keep shorter than `REBALANCE_SCHEDULE`

#### `FUNDING_RATE_LOOKBACK_PERIOD` (Default: 604800 seconds / 7 days)
- **Description**: Historical window for funding rate analysis
- **Range**: 86400-2592000 seconds (1-30 days)
- **Effect**:
  - Shorter = More reactive to recent funding changes
  - Longer = More stable, less prone to short-term anomalies
- **Statistical Impact**: Used to calculate mean and standard deviation

### Transaction Minimums

#### `MIN_BRIDGE_AMOUNT` (Default: 1 USD)
- **Description**: Minimum amount for cross-chain bridges
- **Range**: 1-100 USD
- **Considerations**:
  - Bridge fees make small amounts uneconomical
  - Across Protocol may have higher minimums
  - Set based on bridge fee analysis

#### `MIN_SWAP_AMOUNT` (Default: 1 USD)
- **Description**: Minimum amount for Unichain swaps
- **Range**: 1-50 USD
- **Considerations**:
  - Gas costs on Unichain
  - Slippage on small trades
  - Price impact thresholds

### Gas Management

#### `MIN_ETH_BALANCE` (Default: 0.0025 ETH)
- **Description**: Threshold triggering gas refill
- **Range**: 0.001-0.01 ETH
- **Purpose**: Ensures transactions don't fail due to insufficient gas
- **Per-chain**: Applies to both Unichain and Arbitrum

#### `TARGET_ETH_BALANCE` (Default: 0.005 ETH)
- **Description**: Target ETH balance after refill
- **Range**: 2x to 10x of `MIN_ETH_BALANCE`
- **Optimization**: Higher values reduce refill frequency

### File Configuration

#### `OUTPUT_FILE` (Default: "strategy_state.csv")
- **Description**: CSV file storing position history
- **Format**: JSON data in CSV rows
- **Purpose**: 
  - Dashboard data source
  - Performance analysis
  - Audit trail
- **Growth**: ~1KB per minute with default settings

### Asset Mapping

#### `ASSET_TO_ADDRESS_MAP`
```python
{
    'ETH': '0x4200000000000000000000000000000000000006',  # WETH on Unichain
    'BTC': '0x0555E30da8f98308EdB960aa94C0Db47230d2B9c',  # WBTC
    'UNI': '0x8f187aA05619a017077f5308904739877ce9eA21',  # UNI
    'USDC': '0x078d782b760474a361dda0af3839290b0ef57ad6'  # USDC
}
```
- **Purpose**: Maps asset symbols to Unichain contract addresses
- **Modification**: Add new assets with verified contract addresses

## Parameter Effects and Tuning

### Leverage Optimization

**Conservative Setup** (Lower Risk/Return):
```python
TARGET_LEVERAGE = 2
LEVERAGE_BUFFER = 0.3
REBALANCE_SCHEDULE = 30 * 60  # 30 minutes
```

**Aggressive Setup** (Higher Risk/Return):
```python
TARGET_LEVERAGE = 4
LEVERAGE_BUFFER = 0.7
REBALANCE_SCHEDULE = 15 * 60  # 15 minutes
```

### Market Condition Adjustments

**High Volatility Markets**:
- Decrease `TARGET_LEVERAGE` to 2-2.5
- Decrease `LEVERAGE_BUFFER` to 0.3-0.4
- Decrease `REBALANCE_SCHEDULE` to 5-10 minutes
- Increase `MIN_ETH_BALANCE` for more gas buffer

**Stable Markets**:
- Increase `TARGET_LEVERAGE` to 3.5-4
- Increase `LEVERAGE_BUFFER` to 0.7-1.0
- Increase `REBALANCE_SCHEDULE` to 30-60 minutes
- Standard gas settings acceptable

### Funding Rate Considerations

**Consistently Positive Funding**:
- Increase `FUNDING_RATE_LOOKBACK_PERIOD` to 14-30 days
- Can use higher leverage safely
- Less frequent rebalancing needed

**Variable Funding**:
- Decrease `FUNDING_RATE_LOOKBACK_PERIOD` to 3-7 days
- More conservative leverage
- Tighter rebalancing bounds

### Capital Efficiency

**Small Accounts** (<$10,000):
- Increase `MIN_BRIDGE_AMOUNT` to $10-50
- Increase `MIN_SWAP_AMOUNT` to $10-50
- Longer `REBALANCE_SCHEDULE` to reduce fees
- Lower leverage to avoid liquidation

**Large Accounts** (>$100,000):
- Can use minimum values for amounts
- Shorter rebalance schedules viable
- Consider price impact on large swaps
- May need custom slippage settings

## Recommended Settings

### Production Configuration
```python
# Conservative production settings
TARGET_LEVERAGE = 3
LEVERAGE_BUFFER = 0.5
ASSET = 'ETH'
REBALANCE_SCHEDULE = 20 * 60  # 20 minutes
POSITION_CHECK_SCHEDULE = 60   # 1 minute
FUNDING_RATE_LOOKBACK_PERIOD = 7 * 24 * 60 * 60  # 7 days
MIN_BRIDGE_AMOUNT = 10
MIN_SWAP_AMOUNT = 5
MIN_ETH_BALANCE = 0.005
TARGET_ETH_BALANCE = 0.01
```

### Testing Configuration
```python
# Aggressive testing settings
TARGET_LEVERAGE = 2
LEVERAGE_BUFFER = 0.3
ASSET = 'ETH'
REBALANCE_SCHEDULE = 5 * 60   # 5 minutes
POSITION_CHECK_SCHEDULE = 30   # 30 seconds
FUNDING_RATE_LOOKBACK_PERIOD = 3 * 24 * 60 * 60  # 3 days
MIN_BRIDGE_AMOUNT = 1
MIN_SWAP_AMOUNT = 1
MIN_ETH_BALANCE = 0.01
TARGET_ETH_BALANCE = 0.02
```

### Performance Monitoring

Monitor these metrics to optimize parameters:
1. **Rebalancing Frequency**: Aim for 2-6 per day
2. **Gas Costs**: Should be <5% of funding income
3. **Leverage Variance**: Should stay within buffer 95%+ of time
4. **Funding Capture**: Compare actual vs theoretical funding

### Safety Checks

Before deploying:
1. Verify all addresses in `ASSET_TO_ADDRESS_MAP`
2. Test with small amounts first
3. Ensure `TARGET_ETH_BALANCE` > 2x typical transaction cost
4. Confirm RPC endpoints are reliable
5. Validate API keys work correctly

### Dynamic Adjustment

Consider implementing dynamic parameters based on:
- Current funding rates
- Market volatility (via price feeds)
- Gas prices
- Account size changes

This configuration system provides flexibility to optimize the strategy for different market conditions, risk tolerances, and capital sizes while maintaining safety and efficiency.