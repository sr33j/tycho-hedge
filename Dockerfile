# Multi-stage build to reduce final image size and disk usage during build
FROM rust:1.75-slim as rust-builder

# Install system dependencies for Rust build (including OpenSSL dev packages)
RUN apt-get update && apt-get install -y \
    pkg-config \
    libssl-dev \
    libssl3 \
    openssl \
    build-essential \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set Cargo environment variables to optimize for disk space
ENV CARGO_HOME=/usr/local/cargo
ENV CARGO_TARGET_DIR=/tmp/target
ENV CARGO_INCREMENTAL=0
ENV RUST_BACKTRACE=1

# Create app directory
WORKDIR /app

# Copy only Cargo files first for better layer caching
COPY tycho-swap/Cargo.toml tycho-swap/Cargo.lock tycho-swap/
COPY tycho-swap/rust-toolchain.toml tycho-swap/

# Create dummy source files to build dependencies
RUN mkdir -p tycho-swap/src tycho-swap/bin/service && \
    echo "fn main() {}" > tycho-swap/src/lib.rs && \
    echo "fn main() {}" > tycho-swap/bin/service/main.rs

# Build dependencies (this will be cached unless Cargo files change)
RUN cd tycho-swap && \
    cargo build --release --bin service && \
    rm -rf src bin && \
    rm -rf /tmp/target/release/service

# Copy the actual source code
COPY tycho-swap/ tycho-swap/

# Build the actual application and clean up
RUN cd tycho-swap && \
    cargo build --release --bin service && \
    cp /tmp/target/release/service /usr/local/bin/tycho-service && \
    cargo clean && \
    rm -rf /tmp/target /usr/local/cargo/registry/cache /usr/local/cargo/git/db

# Final stage - Python runtime
FROM python:3.10-slim

# Install runtime dependencies (including OpenSSL for runtime)
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    libssl3 \
    file \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code first
COPY . .

# Create the target directory structure
RUN mkdir -p /app/tycho-swap/target/release

# Copy the built Rust binary from builder stage (this must be AFTER COPY . . to override the host binary)
COPY --from=rust-builder /usr/local/bin/tycho-service /app/tycho-swap/target/release/service

# Remove unnecessary source files to save space (optional cleanup)
RUN rm -rf tycho-swap/src tycho-swap/target/debug 2>/dev/null || true

# Create logs directory
RUN mkdir -p /app/logs

# Make scripts executable
RUN chmod +x docker-entrypoint.sh

# Expose ports for both Tycho service and Streamlit dashboard
EXPOSE 3000 8501

ENTRYPOINT ["./docker-entrypoint.sh"]
