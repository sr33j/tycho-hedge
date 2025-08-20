# Funding Rate Strategy with Tycho

## Architecture Overview

This project consists of two main components:

1. **Tycho Swap Service** (Rust): A long-running service that continuously indexes DEX pools and provides real-time quotes
2. **Strategy Engine** (Python): Executes the funding rate arbitrage strategy by calling the Tycho service

## Docker Setup (Recommended)

The easiest way to run the strategy is using Docker:

### 1. Setup Environment
```bash
# Create .env file from template
cp .env.example .env

# Edit .env with your credentials
nano .env  # or use your preferred editor
```

### 2. Run with Docker
```bash
# Build and run the strategy
docker-compose up --build
```

This single command will:
- Build the Docker container with all dependencies
- Start the Tycho indexing service with auto-restart
- Wait for the service to be ready
- Launch the funding rate strategy with error recovery
- Automatically restart both services on failure

### 3. Monitor and Control
```bash
# View logs
docker-compose logs -f

# Stop the strategy
docker-compose down

# Run with unwind flag
docker-compose run --rm tycho-hedge --unwind

# Restart if needed
docker-compose restart
```

## Manual Setup (Alternative)

If you prefer to run without Docker:

### 1. Start the Tycho Service
```bash
# Use python 3.10
pip install -r requirements.txt

# Create a .env file based on .env.example
cp .env.example .env
# Edit .env with your credentials

# Start the Tycho indexing service (runs continuously)
./start_tycho_service.sh
```

The service will:
- Index DEX pools on your chosen chain (default: Unichain)
- Provide real-time quotes via HTTP API on port 3000
- Maintain up-to-date pool state for fast swaps

### 2. Run the Strategy (in a new terminal)
```bash
# Run the funding rate strategy
python main.py
```

The strategy will make HTTP calls to the running Tycho service for swaps instead of starting the indexer each time.

⚠️ **Current Status**: The service currently provides quotes but swap execution is not yet implemented. The Python client will fall back to the original subprocess method for actual swaps until execution is completed.

### 3. Test the Service (optional)

## Tycho Service API

The service exposes the following endpoints:

- `GET /health` - Check service health and see number of indexed pools
- `POST /quote` - Get best quote for a token swap
- `POST /execute` - Execute a token swap (⚠️ **Not yet implemented** - returns placeholder response)

### Example Usage

```bash
# Check if service is healthy
curl http://localhost:3000/health

# Get a quote
curl -X POST http://localhost:3000/quote \
  -H "Content-Type: application/json" \
  -d '{
    "sell_token": "0x078d782b760474a361dda0af3839290b0ef57ad6",
    "buy_token": "0x4200000000000000000000000000000000000006",
    "sell_amount": 100.0
  }'
```

## Environment Variables

Add these to your `.env` file:

```bash
# Required for Tycho Service
TYCHO_URL=your_tycho_url
TYCHO_API_KEY=your_api_key
UNICHAIN_RPC_URL=your_rpc_url
PRIVATE_KEY=your_private_key
PUBLIC_ADDRESS=your_wallet_address

# Optional
CHAIN=unichain  # Chain to index (default: unichain)
PORT=3000       # Service port (default: 3000)
TYCHO_SERVICE_URL=http://localhost:3000  # Service URL for Python client
RUST_LOG=info   # Rust logging level
```

## Unwind

To unwind the strategy, run the following command:

```bash
python main.py --unwind
```

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