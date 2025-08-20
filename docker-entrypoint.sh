#!/bin/bash
set -e

echo "🐳 Starting Tycho Hedge Strategy in Docker..."

# Create logs directory if it doesn't exist
mkdir -p /app/logs

# Setup logging
LOG_DIR="/app/logs"
TYCHO_LOG="$LOG_DIR/tycho-service.log"
STRATEGY_LOG="$LOG_DIR/strategy.log"
DASHBOARD_LOG="$LOG_DIR/dashboard.log"
MAIN_LOG="$LOG_DIR/main.log"

# Function to log with timestamp
log_with_timestamp() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$MAIN_LOG"
}

# Function to handle cleanup
cleanup() {
    log_with_timestamp "🧹 Cleaning up processes..."
    
    # Kill all background processes
    if [ ! -z "$TYCHO_PID" ] && kill -0 $TYCHO_PID 2>/dev/null; then
        kill $TYCHO_PID 2>/dev/null || true
        wait $TYCHO_PID 2>/dev/null || true
    fi
    
    if [ ! -z "$DASHBOARD_PID" ] && kill -0 $DASHBOARD_PID 2>/dev/null; then
        kill $DASHBOARD_PID 2>/dev/null || true
        wait $DASHBOARD_PID 2>/dev/null || true
    fi
    
    log_with_timestamp "🏁 Cleanup completed"
    exit 0
}

# Function to start dashboard
start_dashboard() {
    log_with_timestamp "📊 Starting Streamlit dashboard..."
    
    # Start dashboard in background with logging
    cd /app
    streamlit run dashboard.py \
        --server.port 8501 \
        --server.address 0.0.0.0 \
        --server.headless true \
        --server.runOnSave false \
        --browser.serverAddress 0.0.0.0 \
        --browser.gatherUsageStats false \
        > "$DASHBOARD_LOG" 2>&1 &
    
    DASHBOARD_PID=$!
    
    # Wait for dashboard to be ready
    local dashboard_ready=false
    for i in {1..15}; do
        if curl -s http://localhost:8501/_stcore/health > /dev/null 2>&1; then
            log_with_timestamp "✅ Dashboard is ready at http://localhost:8501"
            dashboard_ready=true
            break
        fi
        sleep 2
    done
    
    if [ "$dashboard_ready" = false ]; then
        log_with_timestamp "⚠️  Dashboard may not be fully ready, but continuing..."
    fi
}

# Function to restart services on failure
restart_tycho_service() {
    local max_attempts=5
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        log_with_timestamp "🔄 Starting Tycho service (attempt $attempt/$max_attempts)..."
        
        # Check if the binary exists and is executable
        if [ ! -f /app/tycho-swap/target/release/service ]; then
            log_with_timestamp "❌ Service binary not found at /app/tycho-swap/target/release/service"
            log_with_timestamp "📋 Checking for binary in container..."
            ls -la /app/tycho-swap/target/release/ 2>&1 | tee -a "$MAIN_LOG"
            exit 1
        fi
        
        # Check binary architecture
        log_with_timestamp "🔍 Checking service binary architecture..."
        file /app/tycho-swap/target/release/service | tee -a "$MAIN_LOG"
        
        cd /app/tycho-swap
        
        # Set environment variables that start_tycho_service.sh would set
        export CHAIN="${CHAIN:-unichain}"
        export PORT="${PORT:-3000}"
        export RUST_LOG="${RUST_LOG:-info}"
        export RPC_URL="${UNICHAIN_RPC_URL}"
        export PK="${PRIVATE_KEY}"
        
        log_with_timestamp "📝 Environment: CHAIN=$CHAIN, PORT=$PORT, RPC_URL=$RPC_URL"
        
        # Start the service directly with logging
        # Use exec to replace the shell process and better handle signals
        /app/tycho-swap/target/release/service >> "$TYCHO_LOG" 2>&1 &
        TYCHO_PID=$!
        
        # Return to app directory for other commands
        cd /app
        
        # Give the service a moment to start
        sleep 5
        
        # Check if the process is still running
        if ! kill -0 $TYCHO_PID 2>/dev/null; then
            log_with_timestamp "❌ Tycho service failed to start - process died immediately"
            log_with_timestamp "📋 Last 20 lines of Tycho service log:"
            tail -n 20 "$TYCHO_LOG" 2>/dev/null | tee -a "$MAIN_LOG"
            
            attempt=$((attempt + 1))
            if [ $attempt -le $max_attempts ]; then
                log_with_timestamp "⏱️  Waiting 10 seconds before retry..."
                sleep 10
                continue
            else
                exit 1
            fi
        fi
        
        # Wait for service to be ready
        log_with_timestamp "⏳ Waiting for Tycho service to be ready at http://localhost:${PORT:-3000}/health..."
        local ready=false
        for i in {1..30}; do
            # Check if process is still alive
            if ! kill -0 $TYCHO_PID 2>/dev/null; then
                log_with_timestamp "❌ Tycho service process died during startup"
                log_with_timestamp "📋 Last 20 lines of Tycho service log:"
                tail -n 20 "$TYCHO_LOG" 2>/dev/null | tee -a "$MAIN_LOG"
                break
            fi
            
            # Try health check with more verbose output
            if curl -s -f -o /dev/null -w "HTTP %{http_code}" http://localhost:${PORT:-3000}/health > /tmp/health_check.txt 2>&1; then
                log_with_timestamp "✅ Tycho service is ready! $(cat /tmp/health_check.txt)"
                ready=true
                break
            else
                local curl_exit=$?
                log_with_timestamp "🔍 Health check attempt $i/30 failed (curl exit: $curl_exit), retrying..."
                # Show first few attempts in more detail
                if [ $i -le 3 ]; then
                    log_with_timestamp "   Health check response: $(cat /tmp/health_check.txt 2>/dev/null || echo 'no response')"
                fi
            fi
            sleep 10
        done
        
        if [ "$ready" = true ]; then
            break
        else
            log_with_timestamp "❌ Tycho service failed to start (attempt $attempt/$max_attempts)"
            if [ ! -z "$TYCHO_PID" ] && kill -0 $TYCHO_PID 2>/dev/null; then
                kill $TYCHO_PID 2>/dev/null || true
                wait $TYCHO_PID 2>/dev/null || true
            fi
            
            if [ $attempt -eq $max_attempts ]; then
                log_with_timestamp "💥 Failed to start Tycho service after $max_attempts attempts"
                log_with_timestamp "📋 Check $TYCHO_LOG for service logs"
                exit 1
            fi
            
            attempt=$((attempt + 1))
            log_with_timestamp "⏱️  Waiting 10 seconds before retry..."
            sleep 10
        fi
    done
}

# Function to run strategy with restart logic
run_strategy_with_restart() {
    local max_attempts=3
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        log_with_timestamp "📈 Starting strategy (attempt $attempt/$max_attempts)..."
        
        # Run strategy with logging
        cd /app
        if python main.py "$@" >> "$STRATEGY_LOG" 2>&1; then
            log_with_timestamp "✅ Strategy completed successfully"
            break
        else
            local exit_code=$?
            log_with_timestamp "❌ Strategy failed with exit code $exit_code (attempt $attempt/$max_attempts)"
            
            if [ $attempt -eq $max_attempts ]; then
                log_with_timestamp "💥 Strategy failed after $max_attempts attempts"
                exit $exit_code
            fi
            
            attempt=$((attempt + 1))
            log_with_timestamp "⏱️  Waiting 30 seconds before retry..."
            sleep 30
            
            # Check if Tycho service is still running
            if ! curl -s http://localhost:${PORT:-3000}/health > /dev/null 2>&1; then
                log_with_timestamp "🔧 Tycho service appears down, restarting..."
                if [ ! -z "$TYCHO_PID" ] && kill -0 $TYCHO_PID 2>/dev/null; then
                    kill $TYCHO_PID 2>/dev/null || true
                    wait $TYCHO_PID 2>/dev/null || true
                fi
                restart_tycho_service
            fi
            
            # Check if dashboard is still running
            if ! curl -s http://localhost:8501/_stcore/health > /dev/null 2>&1; then
                log_with_timestamp "🔧 Dashboard appears down, restarting..."
                if [ ! -z "$DASHBOARD_PID" ] && kill -0 $DASHBOARD_PID 2>/dev/null; then
                    kill $DASHBOARD_PID 2>/dev/null || true
                    wait $DASHBOARD_PID 2>/dev/null || true
                fi
                start_dashboard
            fi
        fi
    done
}

# Set up signal handlers
trap cleanup SIGTERM SIGINT

# Check if .env file exists
if [ ! -f .env ]; then
    log_with_timestamp "❌ Error: .env file not found. Please mount .env file to /app/.env"
    exit 1
fi

# Load environment variables
source .env

# Validate required environment variables
required_vars=(
    "TYCHO_URL"
    "TYCHO_API_KEY"
    "UNICHAIN_RPC_URL"
    "PRIVATE_KEY"
    "PUBLIC_ADDRESS"
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        log_with_timestamp "❌ Error: Required environment variable $var is not set"
        exit 1
    fi
done

log_with_timestamp "✅ Environment variables validated"

# Start services
log_with_timestamp "🚀 Starting all services..."

# Start the Tycho service with restart logic
restart_tycho_service

# Start the dashboard
start_dashboard

# Run strategy with restart logic
run_strategy_with_restart "$@"

# Final cleanup
cleanup
