# Fuding Rate Strategy with Tycho

## Quickstart
- Use python 3.10
- `pip install -r requirements.txt`
- create a `.env` file based on `.env.example`
- `python main.py`

## Parameters:
- `OUTPUT_FILE`: A csv file in which your position data will be stored.
- `TARGET_LEVERAGE`: The target leverage on the perp 
- `LEVERAGE_BUFFER`: The buffer (+/-) around the leverage to determine if a rebalance is necessary
- `ASSET`: Ticker of the asset to execute the trade (based on Hyperliquid's tickers)
- `REBALANCE_SCHEDULE`: Interval to check if a rebalance is required (in seconds)
- `FUNDING_RATE_LOOKBACK_PERIOD`: Window of funding to look at to determine if the trade should be active (in seconds)
- `MIN_BRIDGE_AMOUNT`: Minimum amount required to execute a bridge (in USD)
- `MIN_SWAP_AMOUNT`: Minimum amount require to execute a swap (in USD)

## Dashboard

A simple Streamlit dashboard is included for monitoring the strategy in real-time.

```bash
# Install extra dependencies (already listed in requirements.txt)
pip install -r requirements.txt

# Run the dashboard
streamlit run dashboard.py
```

The dashboard visualises:

- **PnL** (absolute & percent) relative to the first recorded state
- **AUM** (total value across chains)
- **Price** of the underlying asset
- **Liquidation Price** (if available)
- A neon-green line chart of PnL over selectable time ranges (1D, 1W, 1M, 3M, All)
- A table of recent trades inferred from changes in perp position size

> The dashboard is strictly read-only – it does **not** expose any trade or “Manage Position” actions.