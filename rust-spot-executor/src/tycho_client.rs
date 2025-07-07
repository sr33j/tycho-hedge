use std::collections::HashMap;

use anyhow::{Context, Result};
use tracing::info;
use tycho_common::{models::Chain, Bytes};
use tycho_simulation::{models::Token, utils::load_all_tokens};

pub struct TychoClient {
    url: String,
    api_key: String,
    chain: Chain,
}

impl TychoClient {
    pub fn new(url: String, api_key: String, chain: Chain) -> Self {
        Self { url, api_key, chain }
    }

    pub async fn load_tokens(&self) -> Result<HashMap<Bytes, Token>> {
        info!("Loading tokens from Tycho for chain: {:?}", self.chain);
        
        let tokens = load_all_tokens(
            &self.url,
            false,
            Some(&self.api_key),
            self.chain,
            None,
            None,
        )
        .await;

        info!("Loaded {} tokens from Tycho", tokens.len());
        Ok(tokens)
    }

    pub fn get_default_url(&self) -> Option<String> {
        match self.chain {
            Chain::Ethereum => Some("wss://tycho-indexer.propellerheads.xyz/ethereum".to_string()),
            Chain::Base => Some("wss://tycho-indexer.propellerheads.xyz/base".to_string()),
            Chain::Unichain => Some("wss://tycho-indexer.propellerheads.xyz/unichain".to_string()),
            _ => None,
        }
    }
}