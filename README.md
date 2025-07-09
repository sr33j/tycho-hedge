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