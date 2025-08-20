import asyncio
import aiohttp
from web3 import Web3
from dotenv import load_dotenv
import os
import json

load_dotenv()

USDC_ARB_ADDRESS = '0xaf88d065e77c8cC2239327C5EDb3A432268e5831'
USDC_UNI_ADDRESS = '0x078D782b760474a361dDA0AF3839290b0EF57AD6'
SPOKE_ARB_ADDRESS = '0xe35e9842fceaCA96570B734083f4a58e8F7C5f2A'
SPOKE_UNI_ADDRESS = '0x09aea4b2242abC8bb4BB78D537A67a245A7bEC64'
# ETH addresses (0xEee... is used by Across for native ETH)
ETH_ADDRESS = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'
WETH_ARB_ADDRESS = '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1'
WETH_UNI_ADDRESS = '0x4200000000000000000000000000000000000006'

class AcrossClient:
    def __init__(self):
        self.initialized = False

    async def initialize(self):
        """Initialize Web3 clients and HTTP session."""
        # Arbitrum client
        self.w3_arb = Web3(Web3.HTTPProvider(os.getenv('ARBITRUM_RPC_URL')))
        # Unichain client
        self.w3_uni = Web3(Web3.HTTPProvider(os.getenv('UNICHAIN_RPC_URL')))

        # Contracts
        self.erc20_arb = self.w3_arb.eth.contract(
            address=USDC_ARB_ADDRESS,
            abi=json.load(open('abis/ERC20.json'))
        )
        self.spoke_arb = self.w3_arb.eth.contract(
            address=SPOKE_ARB_ADDRESS,
            abi=json.load(open('abis/SpokePool.json'))
        )
        self.erc20_uni = self.w3_uni.eth.contract(
            address=USDC_UNI_ADDRESS,
            abi=json.load(open('abis/ERC20.json'))
        )
        self.spoke_uni = self.w3_uni.eth.contract(
            address=SPOKE_UNI_ADDRESS,
            abi=json.load(open('abis/SpokePool.json'))
        )

        # Account
        self.account = os.getenv('PUBLIC_ADDRESS')
        self.priv = os.getenv('PRIVATE_KEY')

        # HTTP session for Across API
        self.session = aiohttp.ClientSession()
        self.initialized = True

    async def _wait_for_tx(self, w3: Web3, tx_hash: str):
        """Wait for transaction receipt and ensure success."""
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        if receipt.status != 1:
            raise Exception(f"Transaction {tx_hash.hex()} failed")
        return receipt

    async def _get_quote(self, origin: int, destination: int,
                         input_token: str, output_token: str, amount: int) -> dict:
        """Call Across GET /suggested-fees to retrieve quote."""
        params = {
            "originChainId": origin,
            "destinationChainId": destination,
            "inputToken": input_token,
            "outputToken": output_token,
            "amount": str(amount)
        }
        url = f"https://across.to/api/suggested-fees"
        async with self.session.get(url, params=params) as resp:
            data = await resp.json()
            # The entire response is the quote data, not nested under 'quote'
            if not data or 'outputAmount' not in data:
                raise Exception(f"Invalid quote returned: {data}")
            
            return data

    async def _approve(self, w3: Web3, token_contract, spender: str, amount: int):
        """Approve the SpokePool to spend tokens."""
        # Check token balance first
        token_balance = token_contract.functions.balanceOf(self.account).call()
        if token_balance < amount:
            raise Exception(f"Insufficient token balance. Have: {token_balance}, Need: {amount}")
        
        # Check ETH balance for gas
        eth_balance = w3.eth.get_balance(self.account)
        estimated_gas_cost = 100000 * w3.eth.gas_price  # Conservative estimate
        if eth_balance < estimated_gas_cost:
            raise Exception(f"Insufficient ETH for gas. Have: {w3.from_wei(eth_balance, 'ether')}, Need: {w3.from_wei(estimated_gas_cost, 'ether')}")
        
        tx = token_contract.functions.approve(spender, amount).build_transaction({
            'from': self.account,
            'nonce': w3.eth.get_transaction_count(self.account),
            'gasPrice': w3.eth.gas_price,
        })
        
        # Estimate gas dynamically
        try:
            estimated_gas = w3.eth.estimate_gas(tx)
            tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
        except Exception as gas_error:
            print(f"Gas estimation failed for approval, using fallback: {gas_error}")
            tx['gas'] = 100000  # Fallback
        
        signed = w3.eth.account.sign_transaction(tx, self.priv)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"Token approval transaction sent: {tx_hash.hex()}")
        await self._wait_for_tx(w3, tx_hash)

    async def _execute_deposit(self, w3: Web3, quote: dict, spoke_contract):
        """
        Manually construct and send the depositV3 transaction.
        """
        # Extract fee information from the quote
        relay_fee_total = int(quote.get('totalRelayFee', {}).get('total', '0'))
        output_amount = int(quote.get('outputAmount', '0'))
        fill_deadline = int(quote.get('fillDeadline', '0'))
        exclusivity_deadline = int(quote.get('exclusivityDeadline', '0'))
        exclusive_relayer = quote.get('exclusiveRelayer', '0x0000000000000000000000000000000000000000')
        quote_timestamp = int(quote.get('timestamp', '0'))
        
        # Calculate the input amount (original amount)
        # We need to reverse-engineer this from the quote data
        # Since we know output_amount and fees, input_amount = output_amount + fees
        input_amount = output_amount + relay_fee_total
        
        # Build the depositV3 transaction manually
        deposit_data = {
            'depositor': self.account,
            'recipient': self.account,
            'inputToken': quote['inputToken']['address'],
            'outputToken': quote['outputToken']['address'],
            'inputAmount': input_amount,
            'outputAmount': output_amount,
            'destinationChainId': quote['outputToken']['chainId'],
            'exclusiveRelayer': exclusive_relayer,
            'quoteTimestamp': quote_timestamp,
            'fillDeadline': fill_deadline,
            'exclusivityParameter': 0,
            'message':  b''
        }
        
        # Build the transaction with correct parameter order
        tx = spoke_contract.functions.depositV3(
            deposit_data['depositor'],
            deposit_data['recipient'],
            deposit_data['inputToken'],
            deposit_data['outputToken'],
            deposit_data['inputAmount'],
            deposit_data['outputAmount'],
            deposit_data['destinationChainId'],
            deposit_data['exclusiveRelayer'],
            deposit_data['quoteTimestamp'],
            deposit_data['fillDeadline'],
            deposit_data['exclusivityParameter'],
            deposit_data['message']
        ).build_transaction({
            'from': self.account,
            'nonce': w3.eth.get_transaction_count(self.account),
            'gasPrice': w3.eth.gas_price,
        })
        
        # Estimate gas dynamically
        try:
            estimated_gas = w3.eth.estimate_gas(tx)
            tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
        except Exception as gas_error:
            print(f"Gas estimation failed for deposit, using fallback: {gas_error}")
            tx['gas'] = 500000  # Higher fallback for complex deposit transaction
        
        # Check ETH balance for gas one more time
        eth_balance = w3.eth.get_balance(self.account)
        gas_cost = tx['gas'] * tx['gasPrice']
        if eth_balance < gas_cost:
            raise Exception(f"Insufficient ETH for gas. Have: {w3.from_wei(eth_balance, 'ether')}, Need: {w3.from_wei(gas_cost, 'ether')}")
        
        # Sign and send the transaction
        signed = w3.eth.account.sign_transaction(tx, self.priv)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"Deposit transaction sent: {tx_hash.hex()}, amount: {input_amount}")
        receipt = await self._wait_for_tx(w3, tx_hash)

        # Listen for the V3FundsDeposited event (for depositV3 calls)
        logs = spoke_contract.events.V3FundsDeposited().process_receipt(receipt)
        if not logs:
            return None
        deposit_id = logs[0]['args']['depositId']
        return deposit_id

    async def _get_deposit_status(self, origin: int, deposit_id: int) -> dict:
        """
        Poll GET /deposit/status until fillStatus == 'filled'.
        """
        url = f"https://across.to/api/deposit/status"
        params = {"originChainId": origin, "depositId": deposit_id}
        while True:
            async with self.session.get(url, params=params) as resp:
                data = await resp.json()
                if data.get('status') == 'filled':
                    return data
            await asyncio.sleep(2)

    async def close(self):
        """Clean up HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()
            
    async def bridge_usdc_arbitrum_to_unichain(self, amount: float, max_retries: int = 3) -> bool:
        """
        Arbitrum → Unichain:
          1) getQuote(origin=42161, dest=130, USDC.e→USDC)
          2) approve SpokePool on Arbitrum
          3) execute deposit, get depositId
          4) (optional) wait for fill
        """
        if not self.initialized:
            await self.initialize()
        units = int(amount * 10**6)
        for attempt in range(max_retries):
            try:
                quote = await self._get_quote(
                    origin=42161,
                    destination=130,
                    input_token=USDC_ARB_ADDRESS,
                    output_token=USDC_UNI_ADDRESS,
                    amount=units
                )
                await self._approve(
                    self.w3_arb, self.erc20_arb, SPOKE_ARB_ADDRESS, units
                )
                deposit_id = await self._execute_deposit(
                    self.w3_arb, quote, self.spoke_arb
                )
                await self._get_deposit_status(42161, deposit_id)
                return True
            except Exception as e:
                import traceback as tb 
                tb.print_exc()
                await asyncio.sleep(2 ** attempt)
        return False

    async def bridge_usdc_unichain_to_arbitrum(self, amount: float, max_retries: int = 3) -> bool:
        """
        Unichain → Arbitrum:
          1) getQuote(origin=130, dest=42161, USDC→USDC.e)
          2) approve SpokePool on Unichain
          3) execute deposit, get depositId
          4) (optional) wait for fill
        """
        if not self.initialized:
            await self.initialize()
        units = int(amount * 10**6)
        for attempt in range(max_retries):
            try:
                quote = await self._get_quote(
                    origin=130,
                    destination=42161,
                    input_token=USDC_UNI_ADDRESS,
                    output_token=USDC_ARB_ADDRESS,
                    amount=units
                )
                await self._approve(
                    self.w3_uni, self.erc20_uni, SPOKE_UNI_ADDRESS, units
                )
                deposit_id = await self._execute_deposit(
                    self.w3_uni, quote, self.spoke_uni
                )
                await self._get_deposit_status(130, deposit_id)
                return True
            except Exception as e:
                import traceback as tb 
                tb.print_exc()
                await asyncio.sleep(2 ** attempt)
        return False
    
    async def bridge_weth_unichain_to_arbitrum(self, amount: float, max_retries: int = 3) -> bool:
        """
        Bridge WETH from Unichain to Arbitrum.
        """
        if not self.initialized:
            await self.initialize()
        units = int(amount * 10**18)  # WETH has 18 decimals
        
        # Initialize WETH contract on Unichain
        weth_uni = self.w3_uni.eth.contract(
            address=WETH_UNI_ADDRESS,
            abi=json.load(open('abis/ERC20.json'))
        )
        
        for attempt in range(max_retries):
            try:
                quote = await self._get_quote(
                    origin=130,
                    destination=42161,
                    input_token=WETH_UNI_ADDRESS,
                    output_token=WETH_ARB_ADDRESS,
                    amount=units
                )
                
                # Approve WETH spending
                await self._approve(
                    self.w3_uni, weth_uni, SPOKE_UNI_ADDRESS, units
                )
                
                # Execute deposit
                deposit_id = await self._execute_deposit(
                    self.w3_uni, quote, self.spoke_uni
                )
                await self._get_deposit_status(130, deposit_id)
                return True
            except Exception as e:
                import traceback as tb 
                tb.print_exc()
                await asyncio.sleep(2 ** attempt)
        return False
    
    async def bridge_weth_arbitrum_to_unichain(self, amount: float, max_retries: int = 3) -> bool:
        """
        Bridge WETH from Arbitrum to Unichain.
        """
        if not self.initialized:
            await self.initialize()
        units = int(amount * 10**18)  # WETH has 18 decimals
        
        # Initialize WETH contract on Arbitrum
        weth_arb = self.w3_arb.eth.contract(
            address=WETH_ARB_ADDRESS,
            abi=json.load(open('abis/ERC20.json'))
        )
        
        for attempt in range(max_retries):
            try:
                quote = await self._get_quote(
                    origin=42161,
                    destination=130,
                    input_token=WETH_ARB_ADDRESS,
                    output_token=WETH_UNI_ADDRESS,
                    amount=units
                )
                
                # Approve WETH spending
                await self._approve(
                    self.w3_arb, weth_arb, SPOKE_ARB_ADDRESS, units
                )
                
                # Execute deposit
                deposit_id = await self._execute_deposit(
                    self.w3_arb, quote, self.spoke_arb
                )
                await self._get_deposit_status(42161, deposit_id)
                return True
            except Exception as e:
                import traceback as tb 
                tb.print_exc()
                await asyncio.sleep(2 ** attempt)
        return False
