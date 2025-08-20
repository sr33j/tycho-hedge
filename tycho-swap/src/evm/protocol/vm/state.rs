#![allow(deprecated)]
use std::{
    any::Any,
    collections::{HashMap, HashSet},
    fmt::Debug,
    str::FromStr,
};

use alloy::primitives::{Address, U256};
use itertools::Itertools;
use num_bigint::BigUint;
use revm::DatabaseRef;
use tycho_common::{dto::ProtocolStateDelta, Bytes};

use super::{
    constants::{EXTERNAL_ACCOUNT, MAX_BALANCE},
    erc20_token::{ERC20OverwriteFactory, ERC20Slots, Overwrites},
    models::Capability,
    tycho_simulation_contract::TychoSimulationContract,
};
use crate::{
    evm::{
        engine_db::{
            engine_db_interface::EngineDatabaseInterface, simulation_db::BlockHeader,
            tycho_db::PreCachedDB,
        },
        protocol::{u256_num::u256_to_biguint, utils::bytes_to_address},
        ContractCompiler, SlotId,
    },
    models::{Balances, Token},
    protocol::{
        errors::{SimulationError, TransitionError},
        models::GetAmountOutResult,
        state::ProtocolSim,
    },
};

#[derive(Clone, Debug)]
pub struct EVMPoolState<D: EngineDatabaseInterface + Clone + Debug>
where
    <D as DatabaseRef>::Error: Debug,
    <D as EngineDatabaseInterface>::Error: Debug,
{
    /// The pool's identifier
    id: String,
    /// The pool's token's addresses
    pub tokens: Vec<Bytes>,
    /// The current block, will be used to set vm context
    block: BlockHeader,
    /// The pool's component balances.
    balances: HashMap<Address, U256>,
    /// The contract address for where protocol balances are stored (i.e. a vault contract).
    /// If given, balances will be overwritten here instead of on the pool contract during
    /// simulations. This has been deprecated in favor of `contract_balances`.
    #[deprecated(note = "Use contract_balances instead")]
    balance_owner: Option<Address>,
    /// Spot prices of the pool by token pair
    spot_prices: HashMap<(Address, Address), f64>,
    /// The supported capabilities of this pool
    capabilities: HashSet<Capability>,
    /// Storage overwrites that will be applied to all simulations. They will be cleared
    /// when ``update_pool_state`` is called, i.e. usually at each block. Hence, the name.
    block_lasting_overwrites: HashMap<Address, Overwrites>,
    /// A set of all contract addresses involved in the simulation of this pool.
    involved_contracts: HashSet<Address>,
    /// A map of contracts to their token balances.
    contract_balances: HashMap<Address, HashMap<Address, U256>>,
    /// Allows the specification of custom storage slots for token allowances and
    /// balances. This is particularly useful for token contracts involved in protocol
    /// logic that extends beyond simple transfer functionality.
    /// Each entry also specify the compiler with which the target contract was compiled. This is
    /// later used to compute storage slot for maps.
    token_storage_slots: HashMap<Address, (ERC20Slots, ContractCompiler)>,
    /// Indicates if the protocol uses custom update rules and requires update
    /// triggers to recalculate spot prices ect. Default is to update on all changes on
    /// the pool.
    manual_updates: bool,
    /// The adapter contract. This is used to interact with the protocol when running simulations
    adapter_contract: TychoSimulationContract<D>,
}

impl<D> EVMPoolState<D>
where
    D: EngineDatabaseInterface + Clone + Debug + 'static,
    <D as DatabaseRef>::Error: Debug,
    <D as EngineDatabaseInterface>::Error: Debug,
{
    /// Creates a new instance of `EVMPoolState` with the given attributes, with the ability to
    /// simulate a protocol-agnostic transaction.
    ///
    /// See struct definition of `EVMPoolState` for attribute explanations.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: String,
        tokens: Vec<Bytes>,
        block: BlockHeader,
        component_balances: HashMap<Address, U256>,
        balance_owner: Option<Address>,
        contract_balances: HashMap<Address, HashMap<Address, U256>>,
        spot_prices: HashMap<(Address, Address), f64>,
        capabilities: HashSet<Capability>,
        block_lasting_overwrites: HashMap<Address, Overwrites>,
        involved_contracts: HashSet<Address>,
        token_storage_slots: HashMap<Address, (ERC20Slots, ContractCompiler)>,
        manual_updates: bool,
        adapter_contract: TychoSimulationContract<D>,
    ) -> Self {
        Self {
            id,
            tokens,
            block,
            balances: component_balances,
            balance_owner,
            spot_prices,
            capabilities,
            block_lasting_overwrites,
            involved_contracts,
            contract_balances,
            token_storage_slots,
            manual_updates,
            adapter_contract,
        }
    }

    /// Ensures the pool supports the given capability
    ///
    /// # Arguments
    ///
    /// * `capability` - The capability that we would like to check for.
    ///
    /// # Returns
    ///
    /// * `Result<(), SimulationError>` - Returns `Ok(())` if the capability is supported, or a
    ///   `SimulationError` otherwise.
    fn ensure_capability(&self, capability: Capability) -> Result<(), SimulationError> {
        if !self.capabilities.contains(&capability) {
            return Err(SimulationError::FatalError(format!(
                "capability {:?} not supported",
                capability.to_string()
            )));
        }
        Ok(())
    }
    /// Sets the spot prices for a pool for all possible pairs of the given tokens.
    ///
    /// # Arguments
    ///
    /// * `tokens` - A hashmap of `Token` instances representing the tokens to calculate spot prices
    ///   for.
    ///
    /// # Returns
    ///
    /// * `Result<(), SimulationError>` - Returns `Ok(())` if the spot prices are successfully set,
    ///   or a `SimulationError` if an error occurs during the calculation or processing.
    ///
    /// # Behavior
    ///
    /// This function performs the following steps:
    /// 1. Ensures the pool has the required capability to perform price calculations.
    /// 2. Iterates over all permutations of token pairs (sell token and buy token). For each pair:
    ///    - Retrieves all possible overwrites, considering the maximum balance limit.
    ///    - Calculates the sell amount limit, considering the overwrites.
    ///    - Invokes the adapter contract's `price` function to retrieve the calculated price for
    ///      the token pair, considering the sell amount limit.
    ///    - Processes the price based on whether the `ScaledPrice` capability is present:
    ///       - If `ScaledPrice` is present, uses the price directly from the adapter contract.
    ///       - If `ScaledPrice` is absent, scales the price by adjusting for token decimals.
    ///    - Stores the calculated price in the `spot_prices` map with the token addresses as the
    ///      key.
    /// 3. Returns `Ok(())` upon successful completion or a `SimulationError` upon failure.
    ///
    /// # Usage
    ///
    /// Spot prices need to be set before attempting to retrieve prices using `spot_price`.
    ///
    /// Tip: Setting spot prices on the pool every time the pool actually changes will result in
    /// faster price fetching than if prices are only set immediately before attempting to retrieve
    /// prices.
    pub fn set_spot_prices(
        &mut self,
        tokens: &HashMap<Bytes, Token>,
    ) -> Result<(), SimulationError> {
        self.ensure_capability(Capability::PriceFunction)?;
        for [sell_token_address, buy_token_address] in self
            .tokens
            .iter()
            .permutations(2)
            .map(|p| [p[0], p[1]])
        {
            let sell_token_address = bytes_to_address(sell_token_address)?;
            let buy_token_address = bytes_to_address(buy_token_address)?;
            let overwrites = Some(self.get_overwrites(
                vec![sell_token_address, buy_token_address],
                *MAX_BALANCE / U256::from(100),
            )?);
            let (sell_amount_limit, _) = self.get_amount_limits(
                vec![sell_token_address, buy_token_address],
                overwrites.clone(),
            )?;
            let price_result = self.adapter_contract.price(
                &self.id,
                sell_token_address,
                buy_token_address,
                vec![sell_amount_limit / U256::from(100)],
                self.block.number,
                overwrites,
            )?;

            let price = if self
                .capabilities
                .contains(&Capability::ScaledPrice)
            {
                *price_result.first().ok_or_else(|| {
                    SimulationError::FatalError("Calculated price array is empty".to_string())
                })?
            } else {
                let unscaled_price = price_result.first().ok_or_else(|| {
                    SimulationError::FatalError("Calculated price array is empty".to_string())
                })?;
                let sell_token_decimals = self.get_decimals(tokens, &sell_token_address)?;
                let buy_token_decimals = self.get_decimals(tokens, &buy_token_address)?;
                *unscaled_price * 10f64.powi(sell_token_decimals as i32) /
                    10f64.powi(buy_token_decimals as i32)
            };

            self.spot_prices
                .insert((sell_token_address, buy_token_address), price);
        }
        Ok(())
    }

    fn get_decimals(
        &self,
        tokens: &HashMap<Bytes, Token>,
        sell_token_address: &Address,
    ) -> Result<usize, SimulationError> {
        tokens
            .get(&Bytes::from(sell_token_address.as_slice()))
            .map(|t| t.decimals)
            .ok_or_else(|| {
                SimulationError::FatalError(format!(
                    "Failed to scale spot prices! Pool: {} Token 0x{:x} is not available!",
                    self.id, sell_token_address
                ))
            })
    }

    /// Retrieves the sell and buy amount limit for a given pair of tokens and the given overwrites.
    ///
    /// Attempting to swap an amount of the sell token that exceeds the sell amount limit is not
    /// advised and in most cases will result in a revert.
    ///
    /// # Arguments
    ///
    /// * `tokens` - A vec of tokens, where the first token is the sell token and the second is the
    ///   buy token. The order of tokens in the input vector is significant and determines the
    ///   direction of the price query.
    /// * `overwrites` - A hashmap of overwrites to apply to the simulation.
    ///
    /// # Returns
    ///
    /// * `Result<(U256,U256), SimulationError>` - Returns the sell and buy amount limit as a `U256`
    ///   if successful, or a `SimulationError` on failure.
    fn get_amount_limits(
        &self,
        tokens: Vec<Address>,
        overwrites: Option<HashMap<Address, HashMap<U256, U256>>>,
    ) -> Result<(U256, U256), SimulationError> {
        let limits = self.adapter_contract.get_limits(
            &self.id,
            tokens[0],
            tokens[1],
            self.block.number,
            overwrites,
        )?;

        Ok(limits)
    }

    /// Updates the pool state.
    ///
    /// It is assumed this is called on a new block. Therefore, first the pool's overwrites cache is
    /// cleared, then the balances are updated and the spot prices are recalculated.
    ///
    /// # Arguments
    ///
    /// * `tokens` - A hashmap of token addresses to `Token` instances. This is necessary for
    ///   calculating new spot prices.
    /// * `balances` - A `Balances` instance containing all balance updates on the current block.
    fn update_pool_state(
        &mut self,
        tokens: &HashMap<Bytes, Token>,
        balances: &Balances,
    ) -> Result<(), SimulationError> {
        // clear cache
        self.adapter_contract
            .engine
            .clear_temp_storage();
        self.block_lasting_overwrites.clear();

        // set balances
        if !self.balances.is_empty() {
            // Pool uses component balances for overwrites
            if let Some(bals) = balances
                .component_balances
                .get(&self.id)
            {
                for (token, bal) in bals {
                    let addr = bytes_to_address(token).map_err(|_| {
                        SimulationError::FatalError(format!(
                            "Invalid token address in balance update: {token:?}"
                        ))
                    })?;
                    self.balances
                        .insert(addr, U256::from_be_slice(bal));
                }
            }
        } else {
            // Pool uses contract balances for overwrites
            for contract in &self.involved_contracts {
                if let Some(bals) = balances
                    .account_balances
                    .get(&Bytes::from(contract.as_slice()))
                {
                    let contract_entry = self
                        .contract_balances
                        .entry(*contract)
                        .or_default();
                    for (token, bal) in bals {
                        let addr = bytes_to_address(token).map_err(|_| {
                            SimulationError::FatalError(format!(
                                "Invalid token address in balance update: {token:?}"
                            ))
                        })?;
                        contract_entry.insert(addr, U256::from_be_slice(bal));
                    }
                }
            }
        }

        // reset spot prices
        self.set_spot_prices(tokens)?;
        Ok(())
    }

    fn get_overwrites(
        &self,
        tokens: Vec<Address>,
        max_amount: U256,
    ) -> Result<HashMap<Address, Overwrites>, SimulationError> {
        let token_overwrites = self.get_token_overwrites(tokens, max_amount)?;

        // Merge `block_lasting_overwrites` with `token_overwrites`
        let merged_overwrites =
            self.merge(&self.block_lasting_overwrites.clone(), &token_overwrites);

        Ok(merged_overwrites)
    }

    fn get_token_overwrites(
        &self,
        tokens: Vec<Address>,
        max_amount: U256,
    ) -> Result<HashMap<Address, Overwrites>, SimulationError> {
        let sell_token = &tokens[0].clone(); //TODO: need to make it clearer from the interface
        let mut res: Vec<HashMap<Address, Overwrites>> = Vec::new();
        if !self
            .capabilities
            .contains(&Capability::TokenBalanceIndependent)
        {
            res.push(self.get_balance_overwrites()?);
        }

        let (slots, compiler) = self
            .token_storage_slots
            .get(sell_token)
            .cloned()
            .unwrap_or((
                ERC20Slots::new(SlotId::from(0), SlotId::from(1)),
                ContractCompiler::Solidity,
            ));

        let mut overwrites = ERC20OverwriteFactory::new(*sell_token, slots.clone(), compiler);

        overwrites.set_balance(max_amount, Address::from_slice(&*EXTERNAL_ACCOUNT.0));

        // Set allowance for adapter_address to max_amount
        overwrites.set_allowance(max_amount, self.adapter_contract.address, *EXTERNAL_ACCOUNT);

        res.push(overwrites.get_overwrites());

        // Merge all overwrites into a single HashMap
        Ok(res
            .into_iter()
            .fold(HashMap::new(), |acc, overwrite| self.merge(&acc, &overwrite)))
    }

    /// Gets all balance overwrites for the pool's tokens.
    ///
    /// If the pool uses component balances, the balances are set for the balance owner (if exists)
    /// or for the pool itself. If the pool uses contract balances, the balances are set for the
    /// contracts involved in the pool.
    ///
    /// # Returns
    ///
    /// * `Result<HashMap<Address, Overwrites>, SimulationError>` - Returns a hashmap of address to
    ///   `Overwrites` if successful, or a `SimulationError` on failure.
    fn get_balance_overwrites(&self) -> Result<HashMap<Address, Overwrites>, SimulationError> {
        let mut balance_overwrites: HashMap<Address, Overwrites> = HashMap::new();

        // Use component balances for overrides
        let address = match self.balance_owner {
            Some(owner) => Some(owner),
            None if !self.contract_balances.is_empty() => None,
            None => Some(self.id.parse().map_err(|_| {
                SimulationError::FatalError(
                    "Failed to get balance overwrites: Pool ID is not an address".into(),
                )
            })?),
        };
        if let Some(address) = address {
            for (token, bal) in &self.balances {
                let (slots, compiler) = if self.involved_contracts.contains(token) {
                    self.token_storage_slots
                        .get(token)
                        .cloned()
                        .ok_or_else(|| {
                            SimulationError::FatalError(
                                "Failed to get balance overwrites: Token storage slots not found"
                                    .into(),
                            )
                        })?
                } else {
                    (ERC20Slots::new(SlotId::from(0), SlotId::from(1)), ContractCompiler::Solidity)
                };

                let mut overwrites = ERC20OverwriteFactory::new(*token, slots, compiler);
                overwrites.set_balance(*bal, address);
                balance_overwrites.extend(overwrites.get_overwrites());
            }
        }

        // Use contract balances for overrides (will overwrite component balances if they were set
        // for a contract we explicitly track balances for)
        for (contract, balances) in &self.contract_balances {
            for (token, balance) in balances {
                let (slots, compiler) = self
                    .token_storage_slots
                    .get(token)
                    .cloned()
                    .unwrap_or((
                        ERC20Slots::new(SlotId::from(0), SlotId::from(1)),
                        ContractCompiler::Solidity,
                    ));

                let mut overwrites = ERC20OverwriteFactory::new(*token, slots, compiler);
                overwrites.set_balance(*balance, *contract);
                balance_overwrites.extend(overwrites.get_overwrites());
            }
        }

        Ok(balance_overwrites)
    }

    fn merge(
        &self,
        target: &HashMap<Address, Overwrites>,
        source: &HashMap<Address, Overwrites>,
    ) -> HashMap<Address, Overwrites> {
        let mut merged = target.clone();

        for (key, source_inner) in source {
            merged
                .entry(*key)
                .or_default()
                .extend(source_inner.clone());
        }

        merged
    }

    #[cfg(test)]
    pub fn get_involved_contracts(&self) -> HashSet<Address> {
        self.involved_contracts.clone()
    }

    #[cfg(test)]
    pub fn get_manual_updates(&self) -> bool {
        self.manual_updates
    }

    #[cfg(test)]
    #[deprecated]
    pub fn get_balance_owner(&self) -> Option<Address> {
        self.balance_owner
    }
}

impl<D> ProtocolSim for EVMPoolState<D>
where
    D: EngineDatabaseInterface + Clone + Debug + 'static,
    <D as DatabaseRef>::Error: Debug,
    <D as EngineDatabaseInterface>::Error: Debug,
{
    fn fee(&self) -> f64 {
        todo!()
    }

    fn spot_price(&self, base: &Token, quote: &Token) -> Result<f64, SimulationError> {
        let base_address = bytes_to_address(&base.address)?;
        let quote_address = bytes_to_address(&quote.address)?;
        self.spot_prices
            .get(&(base_address, quote_address))
            .cloned()
            .ok_or(SimulationError::FatalError(format!(
                "Spot price not found for base token {base_address} and quote token {quote_address}"
            )))
    }

    fn get_amount_out(
        &self,
        amount_in: BigUint,
        token_in: &Token,
        token_out: &Token,
    ) -> Result<GetAmountOutResult, SimulationError> {
        let sell_token_address = bytes_to_address(&token_in.address)?;
        let buy_token_address = bytes_to_address(&token_out.address)?;
        let sell_amount = U256::from_be_slice(&amount_in.to_bytes_be());
        let overwrites = self.get_overwrites(
            vec![sell_token_address, buy_token_address],
            *MAX_BALANCE / U256::from(100),
        )?;
        let (sell_amount_limit, _) = self.get_amount_limits(
            vec![sell_token_address, buy_token_address],
            Some(overwrites.clone()),
        )?;
        let (sell_amount_respecting_limit, sell_amount_exceeds_limit) = if self
            .capabilities
            .contains(&Capability::HardLimits) &&
            sell_amount_limit < sell_amount
        {
            (sell_amount_limit, true)
        } else {
            (sell_amount, false)
        };

        let overwrites_with_sell_limit =
            self.get_overwrites(vec![sell_token_address, buy_token_address], sell_amount_limit)?;
        let complete_overwrites = self.merge(&overwrites, &overwrites_with_sell_limit);

        let (trade, state_changes) = self.adapter_contract.swap(
            &self.id,
            sell_token_address,
            buy_token_address,
            false,
            sell_amount_respecting_limit,
            self.block.number,
            Some(complete_overwrites),
        )?;

        let mut new_state = self.clone();

        // Apply state changes to the new state
        for (address, state_update) in state_changes {
            if let Some(storage) = state_update.storage {
                let block_overwrites = new_state
                    .block_lasting_overwrites
                    .entry(address)
                    .or_default();
                for (slot, value) in storage {
                    let slot = U256::from_str(&slot.to_string()).map_err(|_| {
                        SimulationError::FatalError("Failed to decode slot index".to_string())
                    })?;
                    let value = U256::from_str(&value.to_string()).map_err(|_| {
                        SimulationError::FatalError("Failed to decode slot overwrite".to_string())
                    })?;
                    block_overwrites.insert(slot, value);
                }
            }
        }

        // Update spot prices
        let new_price = trade.price;
        if new_price != 0.0f64 {
            new_state
                .spot_prices
                .insert((sell_token_address, buy_token_address), new_price);
            new_state
                .spot_prices
                .insert((buy_token_address, sell_token_address), 1.0f64 / new_price);
        }

        let buy_amount = trade.received_amount;

        if sell_amount_exceeds_limit {
            return Err(SimulationError::InvalidInput(
                format!("Sell amount exceeds limit {sell_amount_limit}"),
                Some(GetAmountOutResult::new(
                    u256_to_biguint(buy_amount),
                    u256_to_biguint(trade.gas_used),
                    Box::new(new_state.clone()),
                )),
            ));
        }
        Ok(GetAmountOutResult::new(
            u256_to_biguint(buy_amount),
            u256_to_biguint(trade.gas_used),
            Box::new(new_state.clone()),
        ))
    }

    fn get_limits(
        &self,
        sell_token: Bytes,
        buy_token: Bytes,
    ) -> Result<(BigUint, BigUint), SimulationError> {
        let sell_token = bytes_to_address(&sell_token)?;
        let buy_token = bytes_to_address(&buy_token)?;
        let overwrites =
            self.get_overwrites(vec![sell_token, buy_token], *MAX_BALANCE / U256::from(100))?;
        let limits = self.get_amount_limits(vec![sell_token, buy_token], Some(overwrites))?;
        Ok((u256_to_biguint(limits.0), u256_to_biguint(limits.1)))
    }

    fn delta_transition(
        &mut self,
        delta: ProtocolStateDelta,
        tokens: &HashMap<Bytes, Token>,
        balances: &Balances,
    ) -> Result<(), TransitionError<String>> {
        if self.manual_updates {
            // Directly check for "update_marker" in `updated_attributes`
            if let Some(marker) = delta
                .updated_attributes
                .get("update_marker")
            {
                // Assuming `marker` is of type `Bytes`, check its value for "truthiness"
                if !marker.is_empty() && marker[0] != 0 {
                    self.update_pool_state(tokens, balances)?;
                }
            }
        } else {
            self.update_pool_state(tokens, balances)?;
        }

        Ok(())
    }

    fn clone_box(&self) -> Box<dyn ProtocolSim> {
        Box::new(self.clone())
    }

    fn as_any(&self) -> &dyn Any {
        self
    }

    fn as_any_mut(&mut self) -> &mut dyn Any {
        self
    }

    fn eq(&self, other: &dyn ProtocolSim) -> bool {
        if let Some(other_state) = other
            .as_any()
            .downcast_ref::<EVMPoolState<PreCachedDB>>()
        {
            self.id == other_state.id
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use alloy::primitives::B256;
    use num_bigint::ToBigUint;
    use num_traits::One;
    use revm::{
        primitives::KECCAK_EMPTY,
        state::{AccountInfo, Bytecode},
    };
    use serde_json::Value;

    use super::*;
    use crate::evm::{
        engine_db::{create_engine, SHARED_TYCHO_DB},
        protocol::vm::{constants::BALANCER_V2, state_builder::EVMPoolStateBuilder},
        simulation::SimulationEngine,
        tycho_models::AccountUpdate,
    };

    fn dai() -> Token {
        Token::new(
            "0x6b175474e89094c44da98b954eedeac495271d0f",
            18,
            "DAI",
            10_000.to_biguint().unwrap(),
        )
    }

    fn bal() -> Token {
        Token::new(
            "0xba100000625a3754423978a60c9317c58a424e3d",
            18,
            "BAL",
            10_000.to_biguint().unwrap(),
        )
    }

    fn dai_addr() -> Address {
        bytes_to_address(&dai().address).unwrap()
    }

    fn bal_addr() -> Address {
        bytes_to_address(&bal().address).unwrap()
    }

    async fn setup_pool_state() -> EVMPoolState<PreCachedDB> {
        let data_str = include_str!("assets/balancer_contract_storage_block_20463609.json");
        let data: Value = serde_json::from_str(data_str).expect("Failed to parse JSON");

        let accounts: Vec<AccountUpdate> = serde_json::from_value(data["accounts"].clone())
            .expect("Expected accounts to match AccountUpdate structure");

        let db = SHARED_TYCHO_DB.clone();
        let engine: SimulationEngine<_> = create_engine(db.clone(), false).unwrap();

        let block = BlockHeader {
            number: 20463609,
            hash: B256::from_str(
                "0x4315fd1afc25cc2ebc72029c543293f9fd833eeb305e2e30159459c827733b1b",
            )
            .unwrap(),
            timestamp: 1722875891,
        };

        for account in accounts.clone() {
            engine.state.init_account(
                account.address,
                AccountInfo {
                    balance: account.balance.unwrap_or_default(),
                    nonce: 0u64,
                    code_hash: KECCAK_EMPTY,
                    code: account
                        .code
                        .clone()
                        .map(|arg0: Vec<u8>| Bytecode::new_raw(arg0.into())),
                },
                None,
                false,
            );
        }
        db.update(accounts, Some(block));

        let tokens = vec![dai().address, bal().address];
        let block = BlockHeader {
            number: 18485417,
            hash: B256::from_str(
                "0x28d41d40f2ac275a4f5f621a636b9016b527d11d37d610a45ac3a821346ebf8c",
            )
            .expect("Invalid block hash"),
            timestamp: 0,
        };

        let pool_id: String =
            "0x4626d81b3a1711beb79f4cecff2413886d461677000200000000000000000011".into();

        let stateless_contracts = HashMap::from([(
            String::from("0x3de27efa2f1aa663ae5d458857e731c129069f29"),
            Some(Vec::new()),
        )]);

        let balances = HashMap::from([
            (dai_addr(), U256::from_str("178754012737301807104").unwrap()),
            (bal_addr(), U256::from_str("91082987763369885696").unwrap()),
        ]);
        let adapter_address =
            Address::from_str("0xA2C5C98A892fD6656a7F39A2f63228C0Bc846270").unwrap();

        EVMPoolStateBuilder::new(pool_id, tokens, block, adapter_address)
            .balances(balances)
            .balance_owner(Address::from_str("0xBA12222222228d8Ba445958a75a0704d566BF2C8").unwrap())
            .adapter_contract_bytecode(Bytecode::new_raw(BALANCER_V2.into()))
            .stateless_contracts(stateless_contracts)
            .build(SHARED_TYCHO_DB.clone())
            .await
            .expect("Failed to build pool state")
    }

    #[tokio::test]
    async fn test_init() {
        let pool_state = setup_pool_state().await;

        let expected_capabilities = vec![
            Capability::SellSide,
            Capability::BuySide,
            Capability::PriceFunction,
            Capability::HardLimits,
        ]
        .into_iter()
        .collect::<HashSet<_>>();

        let capabilities_adapter_contract = pool_state
            .adapter_contract
            .get_capabilities(
                &pool_state.id,
                bytes_to_address(&pool_state.tokens[0]).unwrap(),
                bytes_to_address(&pool_state.tokens[1]).unwrap(),
            )
            .unwrap();

        assert_eq!(capabilities_adapter_contract, expected_capabilities.clone());

        let capabilities_state = pool_state.clone().capabilities;

        assert_eq!(capabilities_state, expected_capabilities.clone());

        for capability in expected_capabilities.clone() {
            assert!(pool_state
                .clone()
                .ensure_capability(capability)
                .is_ok());
        }

        assert!(pool_state
            .clone()
            .ensure_capability(Capability::MarginalPrice)
            .is_err());

        // Verify all tokens are initialized in the engine
        let engine_accounts = pool_state
            .adapter_contract
            .engine
            .state
            .clone()
            .get_account_storage();
        for token in pool_state.tokens.clone() {
            let account = engine_accounts
                .get_account_info(&bytes_to_address(&token).unwrap())
                .unwrap();
            assert_eq!(account.balance, U256::from(0));
            assert_eq!(account.nonce, 0u64);
            assert_eq!(account.code_hash, KECCAK_EMPTY);
            assert!(account.code.is_some());
        }

        // Verify external account is initialized in the engine
        let external_account = engine_accounts
            .get_account_info(&EXTERNAL_ACCOUNT)
            .unwrap();
        assert_eq!(external_account.balance, U256::from(*MAX_BALANCE));
        assert_eq!(external_account.nonce, 0u64);
        assert_eq!(external_account.code_hash, KECCAK_EMPTY);
        assert!(external_account.code.is_none());
    }

    #[tokio::test]
    async fn test_get_amount_out() -> Result<(), Box<dyn std::error::Error>> {
        let pool_state = setup_pool_state().await;

        let result = pool_state
            .get_amount_out(BigUint::from_str("1000000000000000000").unwrap(), &dai(), &bal())
            .unwrap();
        let new_state = result
            .new_state
            .as_any()
            .downcast_ref::<EVMPoolState<PreCachedDB>>()
            .unwrap();
        assert_eq!(result.amount, BigUint::from_str("137780051463393923").unwrap());
        assert_eq!(result.gas, BigUint::from_str("102770").unwrap());
        assert_ne!(new_state.spot_prices, pool_state.spot_prices);
        assert!(pool_state
            .block_lasting_overwrites
            .is_empty());
        Ok(())
    }

    #[tokio::test]
    async fn test_sequential_get_amount_outs() {
        let pool_state = setup_pool_state().await;

        let result = pool_state
            .get_amount_out(BigUint::from_str("1000000000000000000").unwrap(), &dai(), &bal())
            .unwrap();
        let new_state = result
            .new_state
            .as_any()
            .downcast_ref::<EVMPoolState<PreCachedDB>>()
            .unwrap();
        assert_eq!(result.amount, BigUint::from_str("137780051463393923").unwrap());
        assert_eq!(result.gas, BigUint::from_str("102770").unwrap());
        assert_ne!(new_state.spot_prices, pool_state.spot_prices);

        let new_result = new_state
            .get_amount_out(BigUint::from_str("1000000000000000000").unwrap(), &dai(), &bal())
            .unwrap();
        let new_state_second_swap = new_result
            .new_state
            .as_any()
            .downcast_ref::<EVMPoolState<PreCachedDB>>()
            .unwrap();

        assert_eq!(new_result.amount, BigUint::from_str("136964651490065626").unwrap());
        assert_eq!(new_result.gas, BigUint::from_str("70048").unwrap());
        assert_ne!(new_state_second_swap.spot_prices, new_state.spot_prices);
    }

    #[tokio::test]
    async fn test_get_amount_out_dust() {
        let pool_state = setup_pool_state().await;

        let result = pool_state
            .get_amount_out(BigUint::one(), &dai(), &bal())
            .unwrap();

        let new_state = result
            .new_state
            .as_any()
            .downcast_ref::<EVMPoolState<PreCachedDB>>()
            .unwrap();
        assert_eq!(result.amount, BigUint::ZERO);
        assert_eq!(result.gas, 68656.to_biguint().unwrap());
        assert_eq!(new_state.spot_prices, pool_state.spot_prices)
    }

    #[tokio::test]
    async fn test_get_amount_out_sell_limit() {
        let pool_state = setup_pool_state().await;

        let result = pool_state.get_amount_out(
            // sell limit is 100279494253364362835
            BigUint::from_str("100379494253364362835").unwrap(),
            &dai(),
            &bal(),
        );

        assert!(result.is_err());

        match result {
            Err(SimulationError::InvalidInput(msg1, amount_out_result)) => {
                assert_eq!(msg1, "Sell amount exceeds limit 100279494253364362835");
                assert!(amount_out_result.is_some());
            }
            _ => panic!("Test failed: was expecting an Err(SimulationError::RetryDifferentInput(_, _)) value"),
        }
    }

    #[tokio::test]
    async fn test_get_amount_limits() {
        let pool_state = setup_pool_state().await;

        let overwrites = pool_state
            .get_overwrites(
                vec![
                    bytes_to_address(&pool_state.tokens[0]).unwrap(),
                    bytes_to_address(&pool_state.tokens[1]).unwrap(),
                ],
                *MAX_BALANCE / U256::from(100),
            )
            .unwrap();
        let (dai_limit, _) = pool_state
            .get_amount_limits(vec![dai_addr(), bal_addr()], Some(overwrites.clone()))
            .unwrap();
        assert_eq!(dai_limit, U256::from_str("100279494253364362835").unwrap());

        let (bal_limit, _) = pool_state
            .get_amount_limits(
                vec![
                    bytes_to_address(&pool_state.tokens[1]).unwrap(),
                    bytes_to_address(&pool_state.tokens[0]).unwrap(),
                ],
                Some(overwrites),
            )
            .unwrap();
        assert_eq!(bal_limit, U256::from_str("13997408640689987484").unwrap());
    }

    #[tokio::test]
    async fn test_set_spot_prices() {
        let mut pool_state = setup_pool_state().await;

        pool_state
            .set_spot_prices(
                &vec![bal(), dai()]
                    .into_iter()
                    .map(|t| (t.address.clone(), t))
                    .collect(),
            )
            .unwrap();

        let dai_bal_spot_price = pool_state
            .spot_prices
            .get(&(
                bytes_to_address(&pool_state.tokens[0]).unwrap(),
                bytes_to_address(&pool_state.tokens[1]).unwrap(),
            ))
            .unwrap();
        let bal_dai_spot_price = pool_state
            .spot_prices
            .get(&(
                bytes_to_address(&pool_state.tokens[1]).unwrap(),
                bytes_to_address(&pool_state.tokens[0]).unwrap(),
            ))
            .unwrap();
        assert_eq!(dai_bal_spot_price, &0.137_778_914_319_047_9);
        assert_eq!(bal_dai_spot_price, &7.071_503_245_428_246);
    }

    #[tokio::test]
    async fn test_get_balance_overwrites_with_component_balances() {
        let pool_state: EVMPoolState<PreCachedDB> = setup_pool_state().await;

        let overwrites = pool_state
            .get_balance_overwrites()
            .unwrap();

        let dai_address = dai_addr();
        let bal_address = bal_addr();
        assert!(overwrites.contains_key(&dai_address));
        assert!(overwrites.contains_key(&bal_address));
    }

    #[tokio::test]
    async fn test_get_balance_overwrites_with_contract_balances() {
        let mut pool_state: EVMPoolState<PreCachedDB> = setup_pool_state().await;

        let contract_address =
            Address::from_str("0xBA12222222228d8Ba445958a75a0704d566BF2C8").unwrap();

        // Ensure no component balances are used
        pool_state.balances.clear();
        pool_state.balance_owner = None;

        // Set contract balances
        let dai_address = dai_addr();
        let bal_address = bal_addr();
        pool_state.contract_balances = HashMap::from([(
            contract_address,
            HashMap::from([
                (dai_address, U256::from_str("7500000000000000000000").unwrap()), // 7500 DAI
                (bal_address, U256::from_str("1500000000000000000000").unwrap()), // 1500 BAL
            ]),
        )]);

        let overwrites = pool_state
            .get_balance_overwrites()
            .unwrap();

        assert!(overwrites.contains_key(&dai_address));
        assert!(overwrites.contains_key(&bal_address));
    }
}
