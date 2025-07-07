use std::{collections::HashMap, str::FromStr, sync::Arc};

use alloy::{
    network::{Ethereum, EthereumWallet},
    primitives::{Address, B256, U256},
    providers::{
        fillers::{FillProvider, JoinFill, WalletFiller},
        Identity, Provider, ProviderBuilder, RootProvider,
    },
    signers::{local::PrivateKeySigner, SignerSync},
};
use anyhow::{Context, Result};
use num_bigint::BigUint;
use num_traits::cast::ToPrimitive;
use pyo3::prelude::*;
use pyo3_asyncio::tokio::future_into_py;
use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;
use tracing::{info, warn};

use tycho_common::{models::Chain, Bytes};
use tycho_simulation::{
    evm::{
        engine_db::tycho_db::PreCachedDB,
        protocol::{
            filters::uniswap_v4_pool_with_hook_filter,
            u256_num::biguint_to_u256,
            uniswap_v2::state::UniswapV2State,
            uniswap_v3::state::UniswapV3State,
            uniswap_v4::state::UniswapV4State,
        },
        stream::ProtocolStreamBuilder,
    },
    models::Token,
    protocol::models::{BlockUpdate, ProtocolComponent},
    utils::load_all_tokens,
};

use crate::tycho_client::TychoClient;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[pyclass]
pub struct SwapQuote {
    #[pyo3(get)]
    pub amount_out: String,
    #[pyo3(get)]
    pub price: f64,
    #[pyo3(get)]
    pub pool_address: String,
    #[pyo3(get)]
    pub protocol: String,
    #[pyo3(get)]
    pub gas_estimate: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[pyclass]
pub struct SwapResult {
    #[pyo3(get)]
    pub tx_hash: String,
    #[pyo3(get)]
    pub amount_out: String,
    #[pyo3(get)]
    pub gas_used: u64,
    #[pyo3(get)]
    pub success: bool,
}

#[pyclass]
pub struct SpotExecutor {
    tycho_client: Arc<TychoClient>,
    provider: Arc<RwLock<Option<FillProvider<
        JoinFill<Identity, WalletFiller<EthereumWallet>>,
        RootProvider<Ethereum>,
    >>>>,
    all_tokens: Arc<RwLock<HashMap<Bytes, Token>>>,
    pairs: Arc<RwLock<HashMap<String, ProtocolComponent>>>,
    chain: Chain,
}

#[pymethods]
impl SpotExecutor {
    #[new]
    pub fn new(
        tycho_url: String,
        tycho_api_key: String,
        _rpc_url: String,
        _private_key: String,
        chain: String,
    ) -> PyResult<Self> {
        let chain = Chain::from_str(&chain)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("Invalid chain: {}", e)))?;

        let tycho_client = Arc::new(TychoClient::new(tycho_url, tycho_api_key, chain));

        Ok(Self {
            tycho_client,
            provider: Arc::new(RwLock::new(None)),
            all_tokens: Arc::new(RwLock::new(HashMap::new())),
            pairs: Arc::new(RwLock::new(HashMap::new())),
            chain,
        })
    }

    #[pyo3(name = "initialize")]
    pub fn py_initialize<'py>(&mut self, py: Python<'py>) -> PyResult<&'py PyAny> {
        let executor = self.clone();
        future_into_py(py, async move {
            executor.initialize().await
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            Ok(())
        })
    }

    #[pyo3(name = "get_spot_price")]
    pub fn py_get_spot_price<'py>(
        &self,
        py: Python<'py>,
        sell_token: String,
        buy_token: String,
    ) -> PyResult<&'py PyAny> {
        let executor = self.clone();
        future_into_py(py, async move {
            let price = executor.get_spot_price(&sell_token, &buy_token).await
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            Ok(price)
        })
    }

    #[pyo3(name = "get_swap_quote")]
    pub fn py_get_swap_quote<'py>(
        &self,
        py: Python<'py>,
        sell_token: String,
        buy_token: String,
        amount_in: String,
    ) -> PyResult<&'py PyAny> {
        let executor = self.clone();
        future_into_py(py, async move {
            let quote = executor.get_swap_quote(&sell_token, &buy_token, &amount_in).await
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            Ok(quote)
        })
    }

    #[pyo3(name = "execute_swap")]
    pub fn py_execute_swap<'py>(
        &self,
        py: Python<'py>,
        sell_token: String,
        buy_token: String,
        amount_in: String,
        min_amount_out: Option<String>,
    ) -> PyResult<&'py PyAny> {
        let executor = self.clone();
        future_into_py(py, async move {
            let result = executor.execute_swap(&sell_token, &buy_token, &amount_in, min_amount_out.as_deref()).await
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            Ok(result)
        })
    }
}

impl Clone for SpotExecutor {
    fn clone(&self) -> Self {
        Self {
            tycho_client: Arc::clone(&self.tycho_client),
            provider: Arc::clone(&self.provider),
            all_tokens: Arc::clone(&self.all_tokens),
            pairs: Arc::clone(&self.pairs),
            chain: self.chain,
        }
    }
}

impl SpotExecutor {
    pub async fn initialize(&self) -> Result<()> {
        info!("Initializing spot executor for chain: {:?}", self.chain);

        // Load all tokens
        let tokens = self.tycho_client.load_tokens().await?;
        {
            let mut all_tokens = self.all_tokens.write().await;
            *all_tokens = tokens;
        }

        // Initialize provider
        // Note: In production, you would pass the actual private key and RPC URL
        let provider = self.create_provider().await?;
        {
            let mut provider_guard = self.provider.write().await;
            *provider_guard = Some(provider);
        }

        info!("Spot executor initialized successfully");
        Ok(())
    }

    pub async fn get_spot_price(&self, sell_token: &str, buy_token: &str) -> Result<f64> {
        let all_tokens = self.all_tokens.read().await;
        
        let sell_token_address = Bytes::from_str(sell_token)
            .context("Invalid sell token address")?;
        let buy_token_address = Bytes::from_str(buy_token)
            .context("Invalid buy token address")?;

        let sell_token_info = all_tokens.get(&sell_token_address)
            .context("Sell token not found")?;
        let buy_token_info = all_tokens.get(&buy_token_address)
            .context("Buy token not found")?;

        // Get spot price from the best available pool
        let quote = self.get_best_pool_quote(
            sell_token_info,
            buy_token_info,
            &BigUint::from(10u128.pow(sell_token_info.decimals as u32)), // 1 token
        ).await?;

        let amount_out = BigUint::from_str(&quote.amount_out)?;
        let price = self.calculate_price(
            &BigUint::from(10u128.pow(sell_token_info.decimals as u32)),
            &amount_out,
            sell_token_info,
            buy_token_info,
        );

        Ok(price)
    }

    pub async fn get_swap_quote(
        &self,
        sell_token: &str,
        buy_token: &str,
        amount_in: &str,
    ) -> Result<SwapQuote> {
        let all_tokens = self.all_tokens.read().await;
        
        let sell_token_address = Bytes::from_str(sell_token)
            .context("Invalid sell token address")?;
        let buy_token_address = Bytes::from_str(buy_token)
            .context("Invalid buy token address")?;

        let sell_token_info = all_tokens.get(&sell_token_address)
            .context("Sell token not found")?;
        let buy_token_info = all_tokens.get(&buy_token_address)
            .context("Buy token not found")?;

        let amount_in_biguint = BigUint::from_str(amount_in)
            .context("Invalid amount_in format")?;

        let quote = self.get_best_pool_quote(sell_token_info, buy_token_info, &amount_in_biguint).await?;

        Ok(quote)
    }

    pub async fn execute_swap(
        &self,
        sell_token: &str,
        buy_token: &str,
        amount_in: &str,
        _min_amount_out: Option<&str>,
    ) -> Result<SwapResult> {
        // This is a placeholder implementation
        // In production, this would execute the actual swap transaction
        warn!("execute_swap called - this is a placeholder implementation");
        
        let quote = self.get_swap_quote(sell_token, buy_token, amount_in).await?;
        
        Ok(SwapResult {
            tx_hash: "0x0000000000000000000000000000000000000000000000000000000000000000".to_string(),
            amount_out: quote.amount_out,
            gas_used: quote.gas_estimate,
            success: true,
        })
    }

    async fn get_best_pool_quote(
        &self,
        sell_token: &Token,
        buy_token: &Token,
        amount_in: &BigUint,
    ) -> Result<SwapQuote> {
        // This would integrate with the actual Tycho protocol stream
        // For now, return a mock quote
        let amount_out = amount_in * 99u32 / 100u32; // Mock 1% slippage
        
        Ok(SwapQuote {
            amount_out: amount_out.to_string(),
            price: self.calculate_price(amount_in, &amount_out, sell_token, buy_token),
            pool_address: "0x0000000000000000000000000000000000000000".to_string(),
            protocol: "uniswap_v3".to_string(),
            gas_estimate: 150_000,
        })
    }

    fn calculate_price(
        &self,
        amount_in: &BigUint,
        amount_out: &BigUint,
        token_in: &Token,
        token_out: &Token,
    ) -> f64 {
        let decimal_in = amount_in.to_f64().unwrap_or(0.0) / 10f64.powi(token_in.decimals as i32);
        let decimal_out = amount_out.to_f64().unwrap_or(0.0) / 10f64.powi(token_out.decimals as i32);

        if decimal_in > 0.0 {
            decimal_out / decimal_in
        } else {
            0.0
        }
    }

    async fn create_provider(&self) -> Result<FillProvider<
        JoinFill<Identity, WalletFiller<EthereumWallet>>,
        RootProvider<Ethereum>,
    >> {
        // This is a placeholder - in production you'd use actual private key and RPC URL
        let fake_pk = "0x123456789abcdef123456789abcdef123456789abcdef123456789abcdef1234";
        let fake_rpc = "https://sepolia-rpc.scroll.io/";

        let pk = B256::from_str(fake_pk)?;
        let signer = PrivateKeySigner::from_bytes(&pk)?;
        let wallet = EthereumWallet::from(signer);
        
        let provider = ProviderBuilder::default()
            .wallet(wallet)
            .connect_http(fake_rpc.parse()?);

        Ok(provider)
    }
}