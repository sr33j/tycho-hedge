use std::{
    collections::{HashMap, HashSet},
    env,
    str::FromStr,
    sync::Arc,
    time::Duration,
};

use alloy::{
    eips::BlockNumberOrTag,
    network::{Ethereum, EthereumWallet},
    primitives::{Address, Bytes as AlloyBytes, Keccak256, Signature, TxKind, B256, U256},
    providers::{
        fillers::{FillProvider, JoinFill, WalletFiller},
        Identity, Provider, ProviderBuilder, RootProvider,
    },
    rpc::types::{TransactionInput, TransactionRequest},
    signers::{local::PrivateKeySigner, SignerSync},
    sol_types::{eip712_domain, SolStruct, SolValue},
};
use axum::{
    extract::State,
    http::StatusCode,
    response::Json,
    routing::{get, post},
    Router,
};
use dotenv::dotenv;
use foundry_config::NamedChain;
use futures::StreamExt;
use num_bigint::BigUint;

use serde::{Deserialize, Serialize};
use tokio::sync::RwLock;
use tower_http::{cors::CorsLayer, trace::TraceLayer};
use tracing::{info, error};
use tracing_subscriber::EnvFilter;
use tycho_execution::encoding::{
    errors::EncodingError,
    evm::{approvals::permit2::PermitSingle, encoder_builders::TychoRouterEncoderBuilder},
    models,
    models::{EncodedSolution, Solution, Swap, Transaction, UserTransferType},
    tycho_encoder::TychoEncoder,
};

use tycho_common::Bytes;
use tycho_swap::{
    evm::{
        engine_db::tycho_db::PreCachedDB,
        protocol::{
            ekubo::state::EkuboState,
            filters::{balancer_pool_filter, curve_pool_filter, uniswap_v4_pool_with_hook_filter},
            pancakeswap_v2::state::PancakeswapV2State,
            u256_num::biguint_to_u256,
            uniswap_v2::state::UniswapV2State,
            uniswap_v3::state::UniswapV3State,
            uniswap_v4::state::UniswapV4State,
            vm::state::EVMPoolState,
        },
        stream::ProtocolStreamBuilder,
    },
    models::Token,
    protocol::models::{BlockUpdate, ProtocolComponent},
    tycho_client::feed::component_tracker::ComponentFilter,
    tycho_common::models::Chain,
    utils::load_all_tokens,
};

// API Types
#[derive(Serialize, Deserialize, Debug)]
pub struct QuoteRequest {
    pub sell_token: String,
    pub buy_token: String,
    pub sell_amount: f64,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct QuoteResponse {
    pub buy_amount: f64,
    pub buy_amount_raw: String,
    pub price: f64,
    pub best_pool: String,
    pub protocol: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct ExecuteRequest {
    pub sell_token: String,
    pub buy_token: String,
    pub sell_amount: f64,
    pub min_buy_amount: Option<f64>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct ExecuteResponse {
    pub success: bool,
    pub transaction_hash: Option<String>,
    pub error: Option<String>,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct HealthResponse {
    pub status: String,
    pub indexed_pools: usize,
    pub last_block: Option<u64>,
}

// Shared State
#[derive(Clone)]
pub struct ServiceState {
    pub pairs: Arc<RwLock<HashMap<String, ProtocolComponent>>>,
    pub amounts_out: Arc<RwLock<HashMap<String, BigUint>>>,
    pub tokens: Arc<RwLock<HashMap<Bytes, Token>>>,
    pub last_block: Arc<RwLock<Option<u64>>>,
    pub chain: Chain,
    pub provider: Arc<FillProvider<JoinFill<Identity, WalletFiller<EthereumWallet>>, RootProvider<Ethereum>>>,
    pub signer: Arc<PrivateKeySigner>,
    pub chain_id: u64,
}

impl ServiceState {
    pub async fn new(
        chain: Chain,
        provider: FillProvider<JoinFill<Identity, WalletFiller<EthereumWallet>>, RootProvider<Ethereum>>,
        signer: PrivateKeySigner,
        chain_id: u64,
    ) -> Self {
        Self {
            pairs: Arc::new(RwLock::new(HashMap::new())),
            amounts_out: Arc::new(RwLock::new(HashMap::new())),
            tokens: Arc::new(RwLock::new(HashMap::new())),
            last_block: Arc::new(RwLock::new(None)),
            chain,
            provider: Arc::new(provider),
            signer: Arc::new(signer),
            chain_id,
        }
    }
    
    pub fn create_encoder(&self) -> Result<Box<dyn TychoEncoder>, String> {
        TychoRouterEncoderBuilder::new()
            .chain(self.chain)
            .user_transfer_type(UserTransferType::TransferFromPermit2)
            .build()
            .map_err(|e| format!("Failed to build encoder: {:?}", e))
    }

    pub async fn execute_swap(&self, request: &ExecuteRequest) -> Result<String, String> {
        // Get the best quote first
        let quote_request = QuoteRequest {
            sell_token: request.sell_token.clone(),
            buy_token: request.buy_token.clone(),
            sell_amount: request.sell_amount,
        };
        
        let quote = self.get_best_quote(&quote_request).await?;
        
        // Get token information
        let tokens = self.tokens.read().await;
        let pairs = self.pairs.read().await;
        
        let sell_token_address = Bytes::from_str(&request.sell_token)
            .map_err(|e| format!("Invalid sell token address: {}", e))?;
        let buy_token_address = Bytes::from_str(&request.buy_token)
            .map_err(|e| format!("Invalid buy token address: {}", e))?;
        
        let sell_token = tokens.get(&sell_token_address)
            .ok_or("Sell token not found")?.clone();
        let buy_token = tokens.get(&buy_token_address)
            .ok_or("Buy token not found")?.clone();
        
        // Get the best pool component
        let component = pairs.get(&quote.best_pool)
            .ok_or("Best pool not found")?.clone();
        
        // Calculate amounts
        let amount_in = BigUint::from((request.sell_amount * 10f64.powi(sell_token.decimals as i32)) as u128);
        let expected_amount = BigUint::from_str(&quote.buy_amount_raw).unwrap_or_default();
        
        // Use minimum buy amount if provided, otherwise use 0.25% slippage
        let min_amount_out = if let Some(min_buy) = request.min_buy_amount {
            BigUint::from((min_buy * 10f64.powi(buy_token.decimals as i32)) as u128)
        } else {
            // Apply 0.25% slippage
            let bps = BigUint::from(10_000u32);
            let slippage_bps = BigUint::from(25u32); // 0.25% = 25 bps
            let multiplier = &bps - slippage_bps;
            (expected_amount.clone() * &multiplier) / &bps
        };
        
        // Get user address from signer
        let user_address = Bytes::from(self.signer.address().to_vec());
        
        // Create solution with the calculated minimum amount
        let mut solution = create_solution(
            component,
            sell_token.clone(),
            buy_token.clone(),
            amount_in.clone(),
            user_address.clone(),
            expected_amount,
        );
        
        // Override the checked_amount with our calculated minimum
        solution.checked_amount = min_amount_out;
        
        // Create encoder and encode solution
        let (tx, sell_token_address_clone, amount_in_clone) = {
            let encoder = self.create_encoder()?;
            
            // Encode the solution
            let encoded_solutions = encoder.encode_solutions(vec![solution.clone()])
                .map_err(|e| format!("Failed to encode solution: {:?}", e))?;
            
            if encoded_solutions.is_empty() {
                return Err("No encoded solutions generated".to_string());
            }
            
            let encoded_solution = encoded_solutions[0].clone();
            
            // Encode the transaction
            let tx = encode_tycho_router_call(
                self.chain_id,
                encoded_solution,
                &solution,
                self.chain.native_token().address,
                (*self.signer).clone(),
            ).map_err(|e| format!("Failed to encode router call: {:?}", e))?;
            
            (tx, sell_token_address.clone(), amount_in.clone())
        };
        
        // Execute the swap
        execute_swap_transaction(
            &self.provider,
            &amount_in_clone,
            self.signer.address(),
            &sell_token_address_clone,
            tx,
            self.chain_id,
        ).await.map_err(|e| format!("Failed to execute swap: {}", e))
    }

    pub async fn get_best_quote(&self, request: &QuoteRequest) -> Result<QuoteResponse, String> {
        let tokens = self.tokens.read().await;
        let pairs = self.pairs.read().await;
        let amounts_out = self.amounts_out.read().await;

        // Find tokens
        let sell_token_address = Bytes::from_str(&request.sell_token)
            .map_err(|e| format!("Invalid sell token address: {}", e))?;
        let buy_token_address = Bytes::from_str(&request.buy_token)
            .map_err(|e| format!("Invalid buy token address: {}", e))?;

        let sell_token = tokens.get(&sell_token_address)
            .ok_or("Sell token not found")?.clone();
        let buy_token = tokens.get(&buy_token_address)
            .ok_or("Buy token not found")?.clone();

        let amount_in = BigUint::from((request.sell_amount * 10f64.powi(sell_token.decimals as i32)) as u128);

        // Find best pool for this token pair
        let mut best_pool: Option<String> = None;
        let mut best_amount: Option<BigUint> = None;

        for (pool_id, component) in pairs.iter() {
            let pool_tokens = &component.tokens;
            if HashSet::from([&sell_token, &buy_token])
                .is_subset(&HashSet::from_iter(pool_tokens.iter()))
            {
                // Create a key for this specific token pair and pool
                let key = format!("{}:{}:{}", pool_id, sell_token_address, buy_token_address);
                if let Some(amount_out) = amounts_out.get(&key) {
                    // Scale the amount based on the input amount vs the standard 1-unit quote
                    let standard_amount = BigUint::from(10u32.pow(sell_token.decimals as u32));
                    let scaled_amount = if standard_amount > BigUint::from(0u32) {
                        (amount_out * &amount_in) / standard_amount
                    } else {
                        amount_out.clone()
                    };

                    if best_amount.as_ref().map_or(true, |best| &scaled_amount > best) {
                        best_amount = Some(scaled_amount);
                        best_pool = Some(pool_id.clone());
                    }
                }
            }
        }

        if let (Some(pool_id), Some(amount_out)) = (best_pool, best_amount) {
            let component = pairs.get(&pool_id).unwrap();
            let buy_amount_decimal = amount_out.to_string().parse::<f64>().unwrap_or(0.0) / 10f64.powi(buy_token.decimals as i32);
            let price = if request.sell_amount > 0.0 { buy_amount_decimal / request.sell_amount } else { 0.0 };

            Ok(QuoteResponse {
                buy_amount: buy_amount_decimal,
                buy_amount_raw: amount_out.to_string(),
                price,
                best_pool: pool_id,
                protocol: component.protocol_system.clone(),
            })
        } else {
            Err("No suitable pools found for this token pair".to_string())
        }
    }
}

// Indexer task that continuously updates state
async fn indexer_task(state: ServiceState) {
    let tycho_url = env::var("TYCHO_URL").expect("TYCHO_URL environment variable not set");
    let tycho_api_key: String = env::var("TYCHO_API_KEY").unwrap_or_else(|_| "sampletoken".to_string());
    let tvl_filter = ComponentFilter::with_tvl_range(0.0, 100.0);

    info!("Loading tokens from Tycho... {}", tycho_url);
    let all_tokens = load_all_tokens(
        &tycho_url,
        false,
        Some(&tycho_api_key),
        state.chain,
        None,
        None,
    ).await;
    info!("Tokens loaded: {}", all_tokens.len());

    // Store tokens in state
    {
        let mut tokens_guard = state.tokens.write().await;
        *tokens_guard = all_tokens.clone();
    }

    let mut protocol_stream = ProtocolStreamBuilder::new(&tycho_url, state.chain);

    // Configure protocols based on chain
    match state.chain {
        Chain::Ethereum => {
            protocol_stream = protocol_stream
                .exchange::<UniswapV2State>("uniswap_v2", tvl_filter.clone(), None)
                .exchange::<UniswapV2State>("sushiswap_v2", tvl_filter.clone(), None)
                .exchange::<PancakeswapV2State>("pancakeswap_v2", tvl_filter.clone(), None)
                .exchange::<UniswapV3State>("uniswap_v3", tvl_filter.clone(), None)
                .exchange::<UniswapV3State>("pancakeswap_v3", tvl_filter.clone(), None)
                .exchange::<EVMPoolState<PreCachedDB>>(
                    "vm:balancer_v2",
                    tvl_filter.clone(),
                    Some(balancer_pool_filter),
                )
                .exchange::<UniswapV4State>(
                    "uniswap_v4",
                    tvl_filter.clone(),
                    Some(uniswap_v4_pool_with_hook_filter),
                )
                .exchange::<EkuboState>("ekubo_v2", tvl_filter.clone(), None)
                .exchange::<EVMPoolState<PreCachedDB>>(
                    "vm:curve",
                    tvl_filter.clone(),
                    Some(curve_pool_filter),
                );
        }
        Chain::Base | Chain::Unichain => {
            protocol_stream = protocol_stream
                .exchange::<UniswapV2State>("uniswap_v2", tvl_filter.clone(), None)
                .exchange::<UniswapV3State>("uniswap_v3", tvl_filter.clone(), None)
                .exchange::<UniswapV4State>(
                    "uniswap_v4",
                    tvl_filter.clone(),
                    Some(uniswap_v4_pool_with_hook_filter),
                );
        }
        _ => {}
    }

    let mut protocol_stream = protocol_stream
        .auth_key(Some(tycho_api_key.clone()))
        .skip_state_decode_failures(true)
        .set_tokens(all_tokens.clone())
        .await
        .build()
        .await
        .expect("Failed building protocol stream");

    info!("Starting indexer loop...");

    while let Some(message_result) = protocol_stream.next().await {
        let message = match message_result {
            Ok(msg) => msg,
            Err(e) => {
                error!("Error receiving message: {:?}. Continuing to next message...", e);
                continue;
            }
        };

        tokio::task::spawn(update_state(state.clone(), message, all_tokens.clone()));
    }
}

async fn update_state(state: ServiceState, message: BlockUpdate, all_tokens: HashMap<Bytes, Token>) {
    let mut pairs = state.pairs.write().await;
    let mut amounts_out = state.amounts_out.write().await;
    let mut last_block = state.last_block.write().await;

    info!("Received block {}", message.block_number);
    *last_block = Some(message.block_number);

    // Update pairs
    for (id, comp) in message.new_pairs.iter() {
        pairs.entry(id.clone()).or_insert_with(|| comp.clone());
    }
    
    if !message.new_pairs.is_empty() {
        info!("Added {} new pairs to index", message.new_pairs.len());
    }

    if message.states.is_empty() {
        return;
    }

    // Update amounts for all token pairs in updated pools
    for (id, state_update) in message.states.iter() {
        if let Some(component) = pairs.get(id) {
            let tokens = &component.tokens;
            
            // Calculate amounts out for all possible token pairs in this pool
            for i in 0..tokens.len() {
                for j in 0..tokens.len() {
                    if i != j {
                        if let (Some(sell_token), Some(buy_token)) = (
                            all_tokens.get(&tokens[i].address),
                            all_tokens.get(&tokens[j].address)
                        ) {
                            // Use a standard amount for quote calculation (1 unit)
                            let amount_in = BigUint::from(10u32.pow(sell_token.decimals as u32));
                            
                            if let Ok(amount_out) = state_update.get_amount_out(
                                amount_in,
                                sell_token,
                                buy_token
                            ) {
                                let key = format!("{}:{}:{}", id, tokens[i].address, tokens[j].address);
                                amounts_out.insert(key, amount_out.amount);
                            }
                        }
                    }
                }
            }
        }
    }

    info!("Updated state for {} pools, total indexed: {}", message.states.len(), pairs.len());
}

// Utility functions for swap execution
fn create_solution(
    component: ProtocolComponent,
    sell_token: Token,
    buy_token: Token,
    sell_amount: BigUint,
    user_address: Bytes,
    expected_amount: BigUint,
) -> Solution {
    // Prepare data to encode. First we need to create a swap object
    let simple_swap = Swap::new(
        component,
        sell_token.address.clone(),
        buy_token.address.clone(),
        // Split defines the fraction of the amount to be swapped. A value of 0 indicates 100% of
        // the amount or the total remaining balance.
        0f64,
    );

    // Compute a minimum amount out
    //
    // # ⚠️ Important Responsibility Note
    // For maximum security, in production code, this minimum amount out should be computed
    // from a third-party source.
    let slippage = 0.0025; // 0.25% slippage
    let bps = BigUint::from(10_000u32);
    let slippage_percent = BigUint::from((slippage * 10000.0) as u32);
    let multiplier = &bps - slippage_percent;
    let min_amount_out = (expected_amount * &multiplier) / &bps;

    // Then we create a solution object with the previous swap
    Solution {
        sender: user_address.clone(),
        receiver: user_address,
        given_token: sell_token.address,
        given_amount: sell_amount,
        checked_token: buy_token.address,
        exact_out: false, // it's an exact in solution
        checked_amount: min_amount_out,
        swaps: vec![simple_swap],
        ..Default::default()
    }
}

fn encode_tycho_router_call(
    chain_id: u64,
    encoded_solution: EncodedSolution,
    solution: &Solution,
    native_address: Bytes,
    signer: PrivateKeySigner,
) -> Result<Transaction, EncodingError> {
    let p = encoded_solution
        .permit
        .expect("Permit object must be set");
    let permit = PermitSingle::try_from(&p)
        .map_err(|_| EncodingError::InvalidInput("Invalid permit".to_string()))?;
    let signature = sign_permit(chain_id, &p, signer)?;
    let given_amount = biguint_to_u256(&solution.given_amount);
    let min_amount_out = biguint_to_u256(&solution.checked_amount);
    let given_token = Address::from_slice(&solution.given_token);
    let checked_token = Address::from_slice(&solution.checked_token);
    let receiver = Address::from_slice(&solution.receiver);

    let method_calldata = (
        given_amount,
        given_token,
        checked_token,
        min_amount_out,
        false,
        false,
        receiver,
        permit,
        signature.as_bytes().to_vec(),
        encoded_solution.swaps,
    )
        .abi_encode();

    let contract_interaction = encode_input(&encoded_solution.function_signature, method_calldata);
    let value = if solution.given_token == native_address {
        solution.given_amount.clone()
    } else {
        BigUint::ZERO
    };
    Ok(Transaction { to: encoded_solution.interacting_with, value, data: contract_interaction })
}

fn sign_permit(
    chain_id: u64,
    permit_single: &models::PermitSingle,
    signer: PrivateKeySigner,
) -> Result<Signature, EncodingError> {
    let permit2_address = Address::from_str("0x000000000022D473030F116dDEE9F6B43aC78BA3")
        .map_err(|_| EncodingError::FatalError("Permit2 address not valid".to_string()))?;
    let domain = eip712_domain! {
        name: "Permit2",
        chain_id: chain_id,
        verifying_contract: permit2_address,
    };
    let permit_single: PermitSingle = PermitSingle::try_from(permit_single)?;
    let hash = permit_single.eip712_signing_hash(&domain);
    signer
        .sign_hash_sync(&hash)
        .map_err(|e| {
            EncodingError::FatalError(format!("Failed to sign permit2 approval with error: {e}"))
        })
}

pub fn encode_input(selector: &str, mut encoded_args: Vec<u8>) -> Vec<u8> {
    let mut hasher = Keccak256::new();
    hasher.update(selector.as_bytes());
    let selector_bytes = &hasher.finalize()[..4];
    let mut call_data = selector_bytes.to_vec();
    // Remove extra prefix if present (32 bytes for dynamic data)
    // Alloy encoding is including a prefix for dynamic data indicating the offset or length
    // but at this point we don't want that
    if encoded_args.len() > 32 &&
        encoded_args[..32] ==
            [0u8; 31]
                .into_iter()
                .chain([32].to_vec())
                .collect::<Vec<u8>>()
    {
        encoded_args = encoded_args[32..].to_vec();
    }
    call_data.extend(encoded_args);
    call_data
}

async fn get_tx_requests(
    provider: &FillProvider<JoinFill<Identity, WalletFiller<EthereumWallet>>, RootProvider<Ethereum>>,
    amount_in: U256,
    user_address: Address,
    sell_token_address: Address,
    tx: Transaction,
    chain_id: u64,
) -> Result<(TransactionRequest, TransactionRequest), Box<dyn std::error::Error>> {
    let block = provider
        .get_block_by_number(BlockNumberOrTag::Latest)
        .await?
        .ok_or("Block not found")?;

    let base_fee = block
        .header
        .base_fee_per_gas
        .ok_or("Base fee not available")?;
    let max_priority_fee_per_gas = 1_000_000_000u64;
    let max_fee_per_gas = base_fee + max_priority_fee_per_gas;

    let approve_function_signature = "approve(address,uint256)";
    let args = (
        Address::from_str("0x000000000022D473030F116dDEE9F6B43aC78BA3")?,
        amount_in,
    );
    let data = encode_input(approve_function_signature, args.abi_encode());
    let nonce = provider.get_transaction_count(user_address).await?;

    let approval_request = TransactionRequest {
        to: Some(TxKind::Call(sell_token_address)),
        from: Some(user_address),
        value: None,
        input: TransactionInput { input: Some(AlloyBytes::from(data)), data: None },
        gas: Some(100_000u64),
        chain_id: Some(chain_id),
        max_fee_per_gas: Some(max_fee_per_gas.into()),
        max_priority_fee_per_gas: Some(max_priority_fee_per_gas.into()),
        nonce: Some(nonce),
        ..Default::default()
    };

    let swap_request = TransactionRequest {
        to: Some(TxKind::Call(Address::from_slice(&tx.to))),
        from: Some(user_address),
        value: Some(biguint_to_u256(&tx.value)),
        input: TransactionInput { input: Some(AlloyBytes::from(tx.data)), data: None },
        gas: Some(800_000u64),
        chain_id: Some(chain_id),
        max_fee_per_gas: Some(max_fee_per_gas.into()),
        max_priority_fee_per_gas: Some(max_priority_fee_per_gas.into()),
        nonce: Some(nonce + 1),
        ..Default::default()
    };
    Ok((approval_request, swap_request))
}

async fn execute_swap_transaction(
    provider: &FillProvider<JoinFill<Identity, WalletFiller<EthereumWallet>>, RootProvider<Ethereum>>,
    amount_in: &BigUint,
    wallet_address: Address,
    sell_token_address: &Bytes,
    tx: Transaction,
    chain_id: u64,
) -> Result<String, Box<dyn std::error::Error>> {
    info!("Executing approval and swap transactions...");
    let (approval_request, swap_request) = get_tx_requests(
        provider,
        biguint_to_u256(amount_in),
        wallet_address,
        Address::from_slice(sell_token_address),
        tx,
        chain_id,
    ).await?;

    let approval_receipt = provider.send_transaction(approval_request).await?;
    let approval_result = approval_receipt.get_receipt().await?;
    info!(
        "Approval transaction sent with hash: {:?} and status: {:?}",
        approval_result.transaction_hash,
        approval_result.status()
    );

    let swap_receipt = provider.send_transaction(swap_request).await?;
    let swap_result = swap_receipt.get_receipt().await?;
    info!(
        "Swap transaction sent with hash: {:?} and status: {:?}",
        swap_result.transaction_hash,
        swap_result.status()
    );

    if !swap_result.status() {
        return Err(format!(
            "Swap transaction with hash {:?} failed.",
            swap_result.transaction_hash
        ).into());
    }

    Ok(format!("{:?}", swap_result.transaction_hash))
}

// HTTP Handlers
async fn health_handler(State(state): State<ServiceState>) -> Json<HealthResponse> {
    let pairs = state.pairs.read().await;
    let last_block = state.last_block.read().await;
    
    Json(HealthResponse {
        status: "healthy".to_string(),
        indexed_pools: pairs.len(),
        last_block: *last_block,
    })
}

async fn quote_handler(
    State(state): State<ServiceState>,
    Json(request): Json<QuoteRequest>,
) -> Result<Json<QuoteResponse>, StatusCode> {
    match state.get_best_quote(&request).await {
        Ok(quote) => Ok(Json(quote)),
        Err(e) => {
            error!("Quote error: {}", e);
            Err(StatusCode::BAD_REQUEST)
        }
    }
}


#[tokio::main]
async fn main() {
    // Load environment variables
    dotenv().ok();
    
    // Initialize tracing
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .with_target(false)
        .init();

    let chain_str = env::var("CHAIN").unwrap_or_else(|_| "unichain".to_string());
    let chain = Chain::from_str(&chain_str)
        .unwrap_or_else(|_| panic!("Unknown chain {}", chain_str));

    info!("Starting Tycho Swap Service on chain: {:?}", chain);

    // Read private key and create signer
    let swapper_pk = env::var("PRIVATE_KEY").expect("PRIVATE_KEY environment variable not set");
    let pk = B256::from_str(&swapper_pk).expect("Failed to convert swapper pk to B256");
    let signer = PrivateKeySigner::from_bytes(&pk).expect("Failed to create PrivateKeySigner");

    // Create wallet and provider
    let wallet = PrivateKeySigner::from_bytes(&pk).expect("Failed to create wallet signer");
    let tx_signer = EthereumWallet::from(wallet.clone());
    let named_chain = NamedChain::from_str(&chain_str.replace("ethereum", "mainnet"))
        .expect("Invalid chain");
    let chain_id = named_chain as u64;
    
    let rpc_url = env::var("UNICHAIN_RPC_URL").expect("UNICHAIN_RPC_URL env var not set");
    let provider = ProviderBuilder::default()
        .with_chain(named_chain)
        .wallet(tx_signer.clone())
        .connect(&rpc_url)
        .await
        .expect("Failed to connect provider");

    let state = ServiceState::new(chain, provider, signer, chain_id).await;

    // Start indexer task
    let indexer_state = state.clone();
    tokio::spawn(async move {
        indexer_task(indexer_state).await;
    });

    // Wait a moment for indexer to start
    tokio::time::sleep(Duration::from_secs(2)).await;

    // Build HTTP router
    let app = Router::new()
        .route("/health", get(health_handler))
        .route("/quote", post(quote_handler))
        .route("/execute", post(|State(state): State<ServiceState>, Json(request): Json<ExecuteRequest>| async move {
            match state.execute_swap(&request).await {
                Ok(tx_hash) => {
                    info!("Swap executed successfully: {}", tx_hash);
                    Ok::<_, StatusCode>(Json(ExecuteResponse {
                        success: true,
                        transaction_hash: Some(tx_hash),
                        error: None,
                    }))
                }
                Err(e) => {
                    error!("Failed to execute swap: {}", e);
                    Ok::<_, StatusCode>(Json(ExecuteResponse {
                        success: false,
                        transaction_hash: None,
                        error: Some(e),
                    }))
                }
            }
        }))
        .layer(CorsLayer::permissive())
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let port = env::var("PORT").unwrap_or_else(|_| "3000".to_string());
    let addr = format!("0.0.0.0:{}", port);
    
    info!("Server starting on {}", addr);
    
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
} 