from abc import ABC
from collections import defaultdict
from logging import getLogger
from typing import Callable, Union

from eth_utils import to_checksum_address
from tycho_indexer_client import dto
from tycho_indexer_client.dto import ComponentWithState, BlockChanges, HexBytes

from . import AccountUpdate, BlockHeader
from .pool_state import ThirdPartyPool
from .storage import TychoDBSingleton
from .utils import decode_tycho_exchange
from ..models import EVMBlock, EthereumToken

log = getLogger(__name__)


class TychoDecodeError(Exception):
    def __init__(self, msg: str, pool_id: str):
        super().__init__(msg)
        self.pool_id = pool_id


class TychoDecoder(ABC):
    ignored_pools: set
    """Component ids for pools that failed to decode snapshots and whose state deltas must be skipped."""

    def __init__(self):
        self.pool_states = {}
        self.ignored_pools = set()

    @staticmethod
    def decode_id(component_id: str) -> str:
        # default assumption is that the id does not need to be altered
        return component_id


def handle_vm_updates(
    block: EVMBlock,
    account_updates: Union[
        dict[dto.HexBytes, dto.AccountUpdate], dict[dto.HexBytes, dto.ResponseAccount]
    ],
    token_proxy_tokens: dict[HexBytes, HexBytes],
) -> list[AccountUpdate]:
    vm_updates = []
    for address, account_update in account_updates.items():
        # collect contract updates to apply to simulation db
        slots = {int(k): int(v) for k, v in account_update.slots.items()}
        balance = account_update.balance
        code = account_update.code

        new_address = token_proxy_tokens.get(address, address)

        vm_updates.append(
            AccountUpdate(
                address=new_address.hex(),
                chain=account_update.chain,
                slots=slots,
                balance=int(balance) if balance is not None else None,
                code=bytearray(code) if code is not None else None,
                change=account_update.change,
            )
        )
    if vm_updates:
        # apply updates to simulation db
        db = TychoDBSingleton.get_instance()
        block_header = BlockHeader(block.id, block.hash_, int(block.ts.timestamp()))
        db.update(vm_updates, block_header)
    return vm_updates


class ThirdPartyPoolTychoDecoder(TychoDecoder):
    """ThirdPartyPool decoder for protocol messages from the Tycho feed"""

    contract_pools: dict[str, list[str]]
    """Mapping of contracts to the pool ids for the pools they affect"""
    component_pool_id: dict[str, str]
    """Mapping of component ids to their internal pool id"""

    def __init__(
        self,
        token_factory_func: Callable[[list[str]], list[EthereumToken]],
        adapter_contract: str,
        minimum_gas: int,
        trace: bool = False,
    ):
        super().__init__()
        self.contract_pools = defaultdict(list)
        self.component_pool_id = {}
        self.token_factory_func = token_factory_func
        self.adapter_contract = adapter_contract
        self.minimum_gas = minimum_gas
        self.trace = trace
        # Map of tokens that will be mapped to a different address to be accessible by
        # the token proxy
        self._token_proxy_tokens: dict[HexBytes, HexBytes] = dict()
        self._all_tokens: set[HexBytes] = set()
        TychoDBSingleton.initialize()

    @staticmethod
    def _generate_proxy_token_addr(idx: int) -> HexBytes:
        """Generate a proxy token address with trailing badbabe.
        This allows us to easily identify the tokens that had their address changed and ease
        debugging.
        """
        padded_idx = hex(idx)[2:]
        padded_zeroes = "0" * (33 - len(padded_idx))
        return HexBytes(f"0x{padded_zeroes}{padded_idx}badbabe")

    def decode_snapshot(
        self, snapshot: dto.Snapshot, block: EVMBlock
    ) -> dict[str, ThirdPartyPool]:
        decoded_pools = {}
        failed_pools = set()

        all_tokens: set[HexBytes] = {
            t for v in snapshot.states.values() for t in v.component.tokens
        }
        for address in snapshot.vm_storage.keys():
            # Checks if the address is a token and hasn't already identified as a
            # proxy token so it can have a new address.
            if address not in self._token_proxy_tokens and address in all_tokens:
                token_index = len(self._token_proxy_tokens)
                new_address = self._generate_proxy_token_addr(token_index)
                self._token_proxy_tokens[HexBytes(address)] = HexBytes(new_address)
        self._all_tokens.update(all_tokens)

        # Duplicate the state that needs to override the proxy contract state
        token_initial_state: dict[HexBytes, dict[int, int]] = dict()
        for address, account_update in snapshot.vm_storage.items():
            if address in self._token_proxy_tokens:
                slots = {int(k): int(v) for k, v in account_update.slots.items()}
                token_initial_state[address] = slots

        handle_vm_updates(block, snapshot.vm_storage, self._token_proxy_tokens)

        account_balances = {
            account.address: account.token_balances
            for account in snapshot.vm_storage.values()
            if len(account.token_balances) != 0
        }
        for snap in snapshot.states.values():
            try:
                pool = self.decode_pool_state(
                    snap,
                    block,
                    account_balances,
                    token_initial_state,
                    self._token_proxy_tokens,
                )
                decoded_pools[pool.id_] = pool
            except TychoDecodeError as e:
                log.log(
                    5,
                    f"Failed to decode third party snapshot with id {snap.component.id}: {e}",
                )
                failed_pools.add(snap.component.id)
                continue
            except Exception as e:
                log.error(
                    f"Failed to decode third party snapshot with id {snap.component.id}: {e}"
                )
                failed_pools.add(snap.component.id)
                continue

        if decoded_pools or failed_pools:
            self.ignored_pools.update(failed_pools)
            exchange = decode_tycho_exchange(
                next(iter(snapshot.states.values())).component.protocol_system
            )
            log.debug(
                f"Finished decoding {exchange} snapshots: {len(decoded_pools)} succeeded, {len(failed_pools)} failed"
            )

        return decoded_pools

    def decode_pool_state(
        self,
        snapshot: ComponentWithState,
        block: EVMBlock,
        account_balances: dict[HexBytes, dict[HexBytes, HexBytes]] = dict(),
        token_initial_states: dict[HexBytes, dict[int, int]] = dict(),
        token_proxy_tokens: dict[HexBytes, HexBytes] = dict(),
    ) -> ThirdPartyPool:
        component = snapshot.component
        state_attributes = snapshot.state.attributes
        static_attributes = component.static_attributes

        tokens = [t.hex() for t in component.tokens]
        try:
            tokens = self.token_factory_func(tokens)
        except KeyError as e:
            raise TychoDecodeError(f"Unsupported token: {e}", pool_id=component.id)

        # component balances
        balances = self.decode_balances(snapshot.state.balances, tokens)

        # contract balances
        contract_balances = {
            to_checksum_address(addr): self.decode_balances(bals, tokens)
            for addr, bals in account_balances.items()
            if addr in component.contract_ids
        }

        optional_attributes = self.decode_optional_attributes(state_attributes)
        pool_id = component.id
        # kept for backwards compatibility, pool_id is now the component id
        if "pool_id" in static_attributes:
            pool_id = static_attributes.pop("pool_id").decode("utf-8")
            self.component_pool_id[component.id] = pool_id

        manual_updates = static_attributes.get(
            "manual_updates", HexBytes("0x00")
        ) > HexBytes("0x00")
        if not manual_updates:
            # trigger pool updates on contract changes
            for address in component.contract_ids:
                self.contract_pools[address.hex()].append(pool_id)

        filtered_tokens_initial_states = {
            addr: slots
            for addr, slots in token_initial_states.items()
            if addr in component.tokens
        }
        if filtered_tokens_initial_states:
            optional_attributes["token_initial_state"] = filtered_tokens_initial_states
            optional_attributes["token_proxy_tokens"] = token_proxy_tokens

        return ThirdPartyPool(
            id_=pool_id,
            tokens=tuple(tokens),
            balances=balances,
            contract_balances=contract_balances,
            block=block,
            marginal_prices={},
            adapter_contract_path=self.adapter_contract,
            trace=self.trace,
            manual_updates=manual_updates,
            involved_contracts=set(
                to_checksum_address(b.hex()) for b in component.contract_ids
            ),
            **optional_attributes,
        )

    @staticmethod
    def decode_optional_attributes(attributes):
        # Handle optional state attributes
        balance_owner = attributes.get("balance_owner")
        if balance_owner is not None:
            balance_owner = balance_owner.hex()
        stateless_contracts = {}
        index = 0
        while f"stateless_contract_addr_{index}" in attributes:
            encoded_address = attributes[f"stateless_contract_addr_{index}"].hex()
            # Stateless contracts address must be utf-8 encoded
            decoded = bytes.fromhex(
                encoded_address[2:]
                if encoded_address.startswith("0x")
                else encoded_address
            ).decode("utf-8")
            code = (
                value.hex()
                if (value := attributes.get(f"stateless_contract_code_{index}"))
                is not None
                else None
            )
            stateless_contracts[decoded] = code
            index += 1
        return {
            "balance_owner": balance_owner,
            "stateless_contracts": stateless_contracts,
        }

    @staticmethod
    def decode_balances(
        balances_msg: dict[HexBytes, HexBytes], tokens: list[EthereumToken]
    ):
        balances = {}
        for addr, balance in balances_msg.items():
            checksum_addr = to_checksum_address(addr)
            token = next(t for t in tokens if t.address == checksum_addr)
            balances[token.address] = token.from_onchain_amount(
                int(balance)  # balances are big endian encoded
            )
        return balances

    def apply_deltas(
        self, pools: dict[str, ThirdPartyPool], delta_msg: BlockChanges, block: EVMBlock
    ) -> dict[str, ThirdPartyPool]:
        updated_pools = {}

        account_updates = delta_msg.account_updates
        state_updates = delta_msg.state_updates
        component_balance_updates = delta_msg.component_balances
        account_balance_updates = delta_msg.account_balances

        for token in delta_msg.new_tokens.keys():
            self._all_tokens.add(token)
        for address in delta_msg.account_updates.keys():
            if address not in self._token_proxy_tokens and address in self._all_tokens:
                token_index = len(self._token_proxy_tokens)
                new_address = self._generate_proxy_token_addr(token_index)
                self._token_proxy_tokens[HexBytes(address)] = HexBytes(new_address)

        # Update contract changes
        vm_updates = handle_vm_updates(block, account_updates, self._token_proxy_tokens)

        # Update component balances
        for component_id, balance_update in component_balance_updates.items():
            if component_id in self.ignored_pools:
                continue
            pool_id = self.component_pool_id.get(component_id, component_id)
            pool = pools[pool_id]
            for addr, token_balance in balance_update.items():
                checksum_addr = to_checksum_address(addr)
                token = next(t for t in pool.tokens if t.address == checksum_addr)
                balance = token.from_onchain_amount(
                    int.from_bytes(token_balance.balance, "big", signed=False)
                )
                pool.balances[token.address] = balance
            pool.block = block
            updated_pools[pool_id] = pool

        # Update account balances
        for account, token_balances in account_balance_updates.items():
            pools_to_update = self.contract_pools.get(account, [])
            for pool_id in pools_to_update:
                pool = pools[pool_id]
                acc_addr = to_checksum_address(account)
                for addr, token_balance in token_balances.items():
                    checksum_addr = to_checksum_address(addr)
                    token = next(t for t in pool.tokens if t.address == checksum_addr)
                    balance = token.from_onchain_amount(
                        int.from_bytes(token_balance.balance, "big", signed=False)
                    )
                    pool.contract_balances[acc_addr][token.address] = balance
                pool.block = block
                updated_pools[pool_id] = pool

        # Update pools with state attribute changes
        for component_id, pool_update in state_updates.items():
            if component_id in self.ignored_pools:
                continue
            pool_id = self.component_pool_id.get(component_id, component_id)
            pool = updated_pools.get(pool_id) or pools[pool_id]

            attributes = pool_update.updated_attributes
            if "balance_owner" in attributes:
                pool.balance_owner = attributes["balance_owner"]
            # TODO: handle stateless_contracts updates
            pool.block = block

            if not pool.manual_updates or attributes.get("update_marker", False):
                # NOTE - the "update_marker" attribute is used to trigger recalculation of spot prices ect. on
                # protocols with custom update rules (i.e. core contract changes only trigger updates on certain pools
                # ect). This allows us to skip unnecessary simulations when the deltas do not affect prices.
                pool.clear_all_cache()

            updated_pools[pool_id] = pool

        # update pools with contract changes
        for account in vm_updates:
            for pool_id in self.contract_pools.get(account.address, []):
                pool = pools[pool_id]
                pool.block = block
                pool.clear_all_cache()
                updated_pools[pool_id] = pool

        return updated_pools
