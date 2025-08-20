use std::{
    collections::HashMap,
    fmt::Debug,
    sync::{Arc, RwLock},
};

use alloy::{
    primitives::{Address, Bytes, StorageValue, B256, U256},
    providers::{
        fillers::{BlobGasFiller, ChainIdFiller, FillProvider, GasFiller, JoinFill, NonceFiller},
        Provider, RootProvider,
    },
    transports::{RpcError, TransportErrorKind},
};
use revm::{
    context::DBErrorMarker,
    state::{AccountInfo, Bytecode},
    DatabaseRef,
};
use thiserror::Error;
use tracing::{debug, info};

use super::{
    super::account_storage::{AccountStorage, StateUpdate},
    engine_db_interface::EngineDatabaseInterface,
};

/// A wrapper over an actual SimulationDB that allows overriding specific storage slots
pub struct OverriddenSimulationDB<'a, DB: DatabaseRef> {
    /// Wrapped database. Will be queried if a requested item is not found in the overrides.
    pub inner_db: &'a DB,
    /// A mapping from account address to storage.
    /// Storage is a mapping from slot index to slot value.
    pub overrides: &'a HashMap<Address, HashMap<U256, U256>>,
}

impl<'a, DB: DatabaseRef> OverriddenSimulationDB<'a, DB> {
    /// Creates a new OverriddenSimulationDB
    ///
    /// # Arguments
    ///
    /// * `inner_db` - Reference to the inner database.
    /// * `overrides` - Reference to a HashMap containing the storage overrides.
    ///
    /// # Returns
    ///
    /// A new instance of OverriddenSimulationDB.
    pub fn new(inner_db: &'a DB, overrides: &'a HashMap<Address, HashMap<U256, U256>>) -> Self {
        OverriddenSimulationDB { inner_db, overrides }
    }
}

impl<DB: DatabaseRef> DatabaseRef for OverriddenSimulationDB<'_, DB> {
    type Error = DB::Error;

    fn basic_ref(&self, address: Address) -> Result<Option<AccountInfo>, Self::Error> {
        self.inner_db.basic_ref(address)
    }

    fn code_by_hash_ref(&self, code_hash: B256) -> Result<Bytecode, Self::Error> {
        self.inner_db
            .code_by_hash_ref(code_hash)
    }

    fn storage_ref(&self, address: Address, index: U256) -> Result<U256, Self::Error> {
        match self.overrides.get(&address) {
            None => self
                .inner_db
                .storage_ref(address, index),
            Some(slot_overrides) => match slot_overrides.get(&index) {
                Some(value) => {
                    debug!(%address, %index, %value, "Requested storage of account {:x?} slot {}", address, index);
                    Ok(*value)
                }
                None => self
                    .inner_db
                    .storage_ref(address, index),
            },
        }
    }

    fn block_hash_ref(&self, number: u64) -> Result<B256, Self::Error> {
        self.inner_db.block_hash_ref(number)
    }
}

#[derive(Debug, Clone, Copy, Eq, Hash, PartialEq, Default)]
pub struct BlockHeader {
    pub number: u64,
    pub hash: B256,
    pub timestamp: u64,
}

/// A wrapper over an Alloy Provider with local storage cache and overrides.
#[derive(Clone, Debug)]
pub struct SimulationDB<P: Provider + Debug> {
    /// Client to connect to the RPC
    client: Arc<P>,
    /// Cached data
    account_storage: Arc<RwLock<AccountStorage>>,
    /// Current block
    block: Option<BlockHeader>,
    /// Tokio runtime to execute async code
    pub runtime: Option<Arc<tokio::runtime::Runtime>>,
}

pub type EVMProvider = FillProvider<
    JoinFill<
        alloy::providers::Identity,
        JoinFill<GasFiller, JoinFill<BlobGasFiller, JoinFill<NonceFiller, ChainIdFiller>>>,
    >,
    RootProvider,
>;

impl<P: Provider + Debug + 'static> SimulationDB<P> {
    pub fn new(
        client: Arc<P>,
        runtime: Option<Arc<tokio::runtime::Runtime>>,
        block: Option<BlockHeader>,
    ) -> Self {
        Self {
            client,
            account_storage: Arc::new(RwLock::new(AccountStorage::new())),
            block,
            runtime,
        }
    }

    /// Set the block that will be used when querying a node
    pub fn set_block(&mut self, block: Option<BlockHeader>) {
        self.block = block;
    }

    /// Update the simulation state.
    ///
    /// Updates the underlying smart contract storage. Any previously missed account,
    /// which was queried and whose state now is in the account_storage will be cleared.
    ///
    /// # Arguments
    ///
    /// * `updates` - Values for the updates that should be applied to the accounts
    /// * `block` - The newest block
    ///
    /// Returns a state update struct to revert this update.
    pub fn update_state(
        &mut self,
        updates: &HashMap<Address, StateUpdate>,
        block: BlockHeader,
    ) -> HashMap<Address, StateUpdate> {
        info!("Received account state update.");
        let mut revert_updates = HashMap::new();
        self.block = Some(block);
        for (address, update_info) in updates.iter() {
            let mut revert_entry = StateUpdate::default();
            if let Some(current_account) = self
                .account_storage
                .read()
                .unwrap()
                .get_account_info(address)
            {
                revert_entry.balance = Some(current_account.balance);
            }
            if update_info.storage.is_some() {
                let mut revert_storage = HashMap::default();
                for index in update_info
                    .storage
                    .as_ref()
                    .unwrap()
                    .keys()
                {
                    if let Some(s) = self
                        .account_storage
                        .read()
                        .unwrap()
                        .get_permanent_storage(address, index)
                    {
                        revert_storage.insert(*index, s);
                    }
                }
                revert_entry.storage = Some(revert_storage);
            }
            revert_updates.insert(*address, revert_entry);

            self.account_storage
                .write()
                .unwrap()
                .update_account(address, update_info);
        }
        revert_updates
    }

    /// Query information about an Ethereum account.
    /// Gets account information not including storage.
    ///
    /// # Arguments
    ///
    /// * `address` - The Ethereum address to query.
    ///
    /// # Returns
    ///
    /// Returns a `Result` containing either an `AccountInfo` object with balance, nonce, and code
    /// information, or an error of type `SimulationDB<M>::Error` if the query fails.
    fn query_account_info(
        &self,
        address: Address,
    ) -> Result<AccountInfo, <SimulationDB<P> as DatabaseRef>::Error> {
        debug!("Querying account info of {:x?} at block {:?}", address, self.block);

        let (balance, nonce, code) = self.block_on(async {
            let mut balance_request = self.client.get_balance(address);
            let mut nonce_request = self
                .client
                .get_transaction_count(address);
            let mut code_request = self.client.get_code_at(address);

            if let Some(block) = &self.block {
                balance_request = balance_request.number(block.number);
                nonce_request = nonce_request.number(block.number);
                code_request = code_request.number(block.number);
            }

            tokio::join!(balance_request, nonce_request, code_request,)
        });
        let code = Bytecode::new_raw(Bytes::copy_from_slice(&code?));

        Ok(AccountInfo::new(balance?, nonce?, code.hash_slow(), code))
    }

    /// Queries a value from storage at the specified index for a given Ethereum account.
    ///
    /// # Arguments
    ///
    /// * `address` - The Ethereum address of the account.
    /// * `index` - The index of the storage value to query.
    ///
    /// # Returns
    ///
    /// Returns a `Result` containing the value from storage at the specified index as an `U256`,
    /// or an error of type `SimulationDB<M>::Error` if the query fails.
    pub fn query_storage(
        &self,
        address: Address,
        index: U256,
    ) -> Result<StorageValue, <SimulationDB<P> as DatabaseRef>::Error> {
        let storage = self.block_on(async {
            let mut request = self
                .client
                .get_storage_at(address, index);
            if let Some(block) = &self.block {
                request = request.number(block.number);
            }
            request.await.unwrap()
        });

        Ok(storage)
    }

    fn block_on<F: core::future::Future>(&self, f: F) -> F::Output {
        // If we get here and have to block the current thread, we really
        // messed up indexing / filling the storage. In that case this will save us
        // at the price of a very high time penalty.
        match &self.runtime {
            Some(runtime) => runtime.block_on(f),
            None => futures::executor::block_on(f),
        }
    }
}

impl<P: Provider + Debug> EngineDatabaseInterface for SimulationDB<P>
where
    P: Provider + Send + Sync + 'static,
{
    type Error = String;

    /// Sets up a single account
    ///
    /// Full control over setting up an accounts. Allows to set up EOAs as
    /// well as smart contracts.
    ///
    /// # Arguments
    ///
    /// * `address` - Address of the account
    /// * `account` - The account information
    /// * `permanent_storage` - Storage to init the account with this storage can only be updated
    ///   manually.
    /// * `mocked` - Whether this account should be considered mocked. For mocked accounts, nothing
    ///   is downloaded from a node; all data must be inserted manually.
    fn init_account(
        &self,
        address: Address,
        mut account: AccountInfo,
        permanent_storage: Option<HashMap<U256, U256>>,
        mocked: bool,
    ) {
        if account.code.is_some() {
            account.code = Some(account.code.unwrap());
        }

        let mut account_storage = self.account_storage.write().unwrap();

        account_storage.init_account(address, account, permanent_storage, mocked);
    }

    /// Clears temp storage
    ///
    /// It is recommended to call this after a new block is received,
    /// to avoid stored state leading to wrong results.
    fn clear_temp_storage(&mut self) {
        self.account_storage
            .write()
            .unwrap()
            .clear_temp_storage();
    }
}

#[derive(Error, Debug)]
pub enum SimulationDBError {
    #[error("Simulation error: {0} ")]
    SimulationError(String),
    #[error("Not implemented error: {0}")]
    NotImplementedError(String),
}

impl DBErrorMarker for SimulationDBError {}

impl From<RpcError<TransportErrorKind>> for SimulationDBError {
    fn from(err: RpcError<TransportErrorKind>) -> Self {
        SimulationDBError::SimulationError(err.to_string())
    }
}

impl<P: Provider> DatabaseRef for SimulationDB<P>
where
    P: Provider + Debug + Send + Sync + 'static,
{
    type Error = SimulationDBError;

    /// Retrieves basic information about an account.
    ///
    /// This function retrieves the basic account information for the specified address.
    /// If the account is present in the storage, the stored account information is returned.
    /// If the account is not present in the storage, the function queries the account information
    /// from the contract and initializes the account in the storage with the retrieved
    /// information.
    ///
    /// # Arguments
    ///
    /// * `address`: The address of the account to retrieve the information for.
    ///
    /// # Returns
    ///
    /// Returns a `Result` containing an `Option` that holds the account information if it exists.
    /// If the account is not found, `None` is returned.
    ///
    /// # Errors
    ///
    /// Returns an error if there was an issue querying the account information from the contract or
    /// accessing the storage.
    ///
    /// # Notes
    ///
    /// * If the account is present in the storage, the function returns a clone of the stored
    ///   account information.
    ///
    /// * If the account is not present in the storage, the function queries the account information
    ///   from the contract, initializes the account in the storage with the retrieved information,
    ///   and returns a clone of the account information.
    fn basic_ref(&self, address: Address) -> Result<Option<AccountInfo>, Self::Error> {
        if let Some(account) = self
            .account_storage
            .read()
            .unwrap()
            .get_account_info(&address)
        {
            return Ok(Some(account.clone()));
        }
        let account_info = self.query_account_info(address)?;
        self.init_account(address, account_info.clone(), None, false);
        Ok(Some(account_info))
    }

    fn code_by_hash_ref(&self, _code_hash: B256) -> Result<Bytecode, Self::Error> {
        Err(SimulationDBError::NotImplementedError(
            "Code by hash is not implemented in SimulationDB".to_string(),
        ))
    }

    /// Retrieves the storage value at the specified address and index.
    ///
    /// If we don't know the value, and the accessed contract is mocked, the function returns
    /// an empty slot instead of querying a node, to avoid potentially returning garbage values.
    ///
    /// # Arguments
    ///
    /// * `address`: The address of the contract to retrieve the storage value from.
    /// * `index`: The index of the storage value to retrieve.
    ///
    /// # Returns
    ///
    /// Returns a `Result` containing the storage value if it exists. If the contract is mocked
    /// and the storage value is not found locally, an empty slot is returned as `U256::ZERO`.
    ///
    /// # Errors
    ///
    /// Returns an error if there was an issue querying the storage value from the contract or
    /// accessing the storage.
    ///
    /// # Notes
    ///
    /// * If the contract is present locally and is mocked, the function first checks if the storage
    ///   value exists locally. If found, it returns the stored value. If not found, it returns an
    ///   empty slot. Mocked contracts are not expected to have valid storage values, so the
    ///   function does not query a node in this case.
    ///
    /// * If the contract is present locally and is not mocked, the function checks if the storage
    ///   value exists locally. If found, it returns the stored value. If not found, it queries the
    ///   storage value from a node, stores it locally, and returns it.
    ///
    /// * If the contract is not present locally, the function queries the account info and storage
    ///   value from a node, initializes the account locally with the retrieved information, and
    ///   returns the storage value.
    fn storage_ref(&self, address: Address, index: U256) -> Result<U256, Self::Error> {
        debug!("Requested storage of account {:x?} slot {}", address, index);
        let is_mocked; // will be None if we don't have this account at all
        {
            let account_storage = self.account_storage.read().unwrap();
            // This scope is to not make two simultaneous borrows
            is_mocked = account_storage.is_mocked_account(&address);
            if let Some(storage_value) = account_storage.get_storage(&address, &index) {
                debug!(
                    "Got value locally. This is a {} account. Value: {}",
                    (if is_mocked.unwrap_or(false) { "mocked" } else { "non-mocked" }),
                    storage_value
                );
                return Ok(storage_value);
            }
        }
        // At this point we know we don't have data for this storage slot.
        match is_mocked {
            Some(true) => {
                debug!("This is a mocked account for which we don't have data. Returning zero.");
                Ok(U256::ZERO)
            }
            Some(false) => {
                let storage_value = self.query_storage(address, index)?;
                let mut account_storage = self.account_storage.write().unwrap();

                account_storage.set_temp_storage(address, index, storage_value);
                debug!(
                    "This is a non-mocked account for which we didn't have data. Fetched value: {}",
                    storage_value
                );
                Ok(storage_value)
            }
            None => {
                let account_info = self.query_account_info(address)?;
                let storage_value = self.query_storage(address, index)?;
                self.init_account(address, account_info, None, false);
                let mut account_storage = self.account_storage.write().unwrap();
                account_storage.set_temp_storage(address, index, storage_value);
                debug!("This is non-initialised account. Fetched value: {}", storage_value);
                Ok(storage_value)
            }
        }
    }

    /// If block header is set, returns the hash. Otherwise returns a zero hash
    /// instead of querying a node.
    fn block_hash_ref(&self, _number: u64) -> Result<B256, Self::Error> {
        match &self.block {
            Some(header) => Ok(header.hash),
            None => Ok(B256::ZERO),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{error::Error, str::FromStr};

    use rstest::rstest;

    use super::*;
    use crate::evm::engine_db::utils::{get_client, get_runtime};

    #[rstest]
    fn test_query_storage_latest_block() -> Result<(), Box<dyn Error>> {
        let db = SimulationDB::new(get_client(None), get_runtime(), None);
        let address = Address::from_str("0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc")?;
        let index = U256::from_limbs_slice(&[8]);
        db.init_account(address, AccountInfo::default(), None, false);

        db.query_storage(address, index)
            .unwrap();

        // There is no assertion, but has the querying failed, we would have panicked by now.
        // This test is not deterministic as it depends on the current state of the blockchain.
        // See the next test where we do this for a specific block.
        Ok(())
    }

    #[rstest]
    fn test_query_account_info() {
        let mut db = SimulationDB::new(get_client(None), get_runtime(), None);
        let block = BlockHeader {
            number: 20308186,
            hash: B256::from_str(
                "0x61c51e3640b02ae58a03201be0271e84e02dac8a4826501995cbe4da24174b52",
            )
            .unwrap(),
            timestamp: 234,
        };
        db.set_block(Some(block));
        let address = Address::from_str("0x168b93113fe5902c87afaecE348581A1481d0f93").unwrap();
        db.init_account(address, AccountInfo::default(), None, false);

        let account_info = db.query_account_info(address).unwrap();

        assert_eq!(account_info.balance, U256::from_str("6246978663692389").unwrap());
        assert_eq!(account_info.nonce, 17);
    }

    #[rstest]
    fn test_mock_account_get_acc_info() {
        let db = SimulationDB::new(get_client(None), get_runtime(), None);
        let mock_acc_address =
            Address::from_str("0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc").unwrap();
        db.init_account(mock_acc_address, AccountInfo::default(), None, true);

        let acc_info = db
            .basic_ref(mock_acc_address)
            .unwrap()
            .unwrap();

        assert_eq!(
            db.account_storage
                .read()
                .unwrap()
                .get_account_info(&mock_acc_address)
                .unwrap(),
            &acc_info
        );
    }

    #[rstest]
    fn test_mock_account_get_storage() {
        let db = SimulationDB::new(get_client(None), get_runtime(), None);
        let mock_acc_address =
            Address::from_str("0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc").unwrap();
        let storage_address = U256::ZERO;
        db.init_account(mock_acc_address, AccountInfo::default(), None, true);

        let storage = db
            .storage_ref(mock_acc_address, storage_address)
            .unwrap();

        assert_eq!(storage, U256::ZERO);
    }

    #[rstest]
    fn test_update_state() {
        let mut db = SimulationDB::new(get_client(None), get_runtime(), None);
        let address = Address::from_str("0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc").unwrap();
        db.init_account(address, AccountInfo::default(), None, false);

        let mut new_storage = HashMap::default();
        let new_storage_value_index = U256::from_limbs_slice(&[123]);
        new_storage.insert(new_storage_value_index, new_storage_value_index);
        let new_balance = U256::from_limbs_slice(&[500]);
        let update = StateUpdate { storage: Some(new_storage), balance: Some(new_balance) };
        let mut updates = HashMap::default();
        updates.insert(address, update);
        let new_block = BlockHeader { number: 1, hash: B256::default(), timestamp: 234 };

        let reverse_update = db.update_state(&updates, new_block);

        assert_eq!(
            db.account_storage
                .read()
                .unwrap()
                .get_storage(&address, &new_storage_value_index)
                .unwrap(),
            new_storage_value_index
        );
        assert_eq!(
            db.account_storage
                .read()
                .unwrap()
                .get_account_info(&address)
                .unwrap()
                .balance,
            new_balance
        );
        assert_eq!(db.block.unwrap().number, 1);

        assert_eq!(
            reverse_update
                .get(&address)
                .unwrap()
                .balance
                .unwrap(),
            AccountInfo::default().balance
        );
        assert_eq!(
            reverse_update
                .get(&address)
                .unwrap()
                .storage,
            Some(HashMap::default())
        );
    }

    #[rstest]
    fn test_overridden_db() {
        let db = SimulationDB::new(get_client(None), get_runtime(), None);
        let slot1 = U256::from_limbs_slice(&[1]);
        let slot2 = U256::from_limbs_slice(&[2]);
        let orig_value1 = U256::from_limbs_slice(&[100]);
        let orig_value2 = U256::from_limbs_slice(&[200]);
        let original_storage: HashMap<U256, U256> = [(slot1, orig_value1), (slot2, orig_value2)]
            .iter()
            .cloned()
            .collect();

        let address1 = Address::from_str("0000000000000000000000000000000000000001").unwrap();
        let address2 = Address::from_str("0000000000000000000000000000000000000002").unwrap();
        let address3 = Address::from_str("0000000000000000000000000000000000000003").unwrap();

        // override slot 1 of address 2
        // and slot 1 of address 3 which doesn't exist in the original DB
        db.init_account(address1, AccountInfo::default(), Some(original_storage.clone()), false);
        db.init_account(address2, AccountInfo::default(), Some(original_storage), false);

        let overridden_value1 = U256::from_limbs_slice(&[101]);
        let mut overrides: HashMap<Address, HashMap<U256, U256>> = HashMap::new();
        overrides.insert(
            address2,
            [(slot1, overridden_value1)]
                .iter()
                .cloned()
                .collect(),
        );
        overrides.insert(
            address3,
            [(slot1, overridden_value1)]
                .iter()
                .cloned()
                .collect(),
        );

        let overriden_db = OverriddenSimulationDB::new(&db, &overrides);

        assert_eq!(
            overriden_db
                .storage_ref(address1, slot1)
                .expect("Value should be available"),
            orig_value1,
            "Slots of non-overridden account should hold original values."
        );

        assert_eq!(
            overriden_db
                .storage_ref(address1, slot2)
                .expect("Value should be available"),
            orig_value2,
            "Slots of non-overridden account should hold original values."
        );

        assert_eq!(
            overriden_db
                .storage_ref(address2, slot1)
                .expect("Value should be available"),
            overridden_value1,
            "Overridden slot of overridden account should hold an overridden value."
        );

        assert_eq!(
            overriden_db
                .storage_ref(address2, slot2)
                .expect("Value should be available"),
            orig_value2,
            "Non-overridden slot of an account with other slots overridden \
            should hold an original value."
        );

        assert_eq!(
            overriden_db
                .storage_ref(address3, slot1)
                .expect("Value should be available"),
            overridden_value1,
            "Overridden slot of an overridden non-existent account should hold an overriden value."
        );
    }
}
