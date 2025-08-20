#!/bin/bash

# Tycho Swap Service Startup Script
set -e

echo "🚀 Starting Tycho Swap Service..."

# Check if .env file exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found. Please create .env with required environment variables."
    exit 1
fi

# Load environment variables
source .env

# Check required environment variables
required_vars=(
    "TYCHO_URL"
    "TYCHO_API_KEY"
    "UNICHAIN_RPC_URL"
    "PRIVATE_KEY"
    "PUBLIC_ADDRESS"
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ Error: Required environment variable $var is not set"
        exit 1
    fi
done

# Set defaults for optional variables
export CHAIN="${CHAIN:-unichain}"
export PORT="${PORT:-3000}"
export RUST_LOG="${RUST_LOG:-info}"
export RPC_URL="${UNICHAIN_RPC_URL}"
export PK="${PRIVATE_KEY}"

echo "✅ Environment variables validated"
echo "📍 Chain: $CHAIN"
echo "🌐 Port: $PORT"
echo "🔗 Tycho URL: $TYCHO_URL"

# Build the service if not already built
if [ ! -f "tycho-swap/target/release/service" ]; then
    echo "🔨 Building Tycho service (this may take a few minutes)..."
    cd tycho-swap
    cargo build --release --bin service
    cd ..
    echo "✅ Service built successfully"
fi

# Start the service
echo "🔄 Starting Tycho Swap Service on port $PORT..."
echo "💡 Service will be available at http://localhost:$PORT"
echo "📊 Health check: http://localhost:$PORT/health"
echo "📋 Quote endpoint: POST http://localhost:$PORT/quote"
echo "⚡ Execute endpoint: POST http://localhost:$PORT/execute"
echo ""
echo "🔍 Press Ctrl+C to stop the service"

cd tycho-swap
exec ./target/release/service 