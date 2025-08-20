import asyncio
import logging
from typing import Optional, Tuple
import structlog
from web3 import Web3
import os
import json
from dotenv import load_dotenv

load_dotenv()

logger = structlog.get_logger()


class GasManager:
    def __init__(self, tycho_client, perp_client, bridge_client, min_eth_balance: float, target_eth_balance: float, 
                 eth_address: str, weth_address: str, asset_to_address_map: dict, asset: str):
        self.tycho_client = tycho_client
        self.perp_client = perp_client
        self.bridge_client = bridge_client
        self.min_eth_balance = min_eth_balance
        self.target_eth_balance = target_eth_balance
        self.eth_address = eth_address
        self.weth_address = weth_address
        self.asset_to_address_map = asset_to_address_map
        self.asset = asset
        self.w3_arb = None  # Will be initialized when needed

    async def check_and_refill_gas(self) -> bool:
        """
        Check native ETH balance on both chains and refill if below threshold.
        Returns True if gas is sufficient or successfully refilled, False otherwise.
        """
        try:
            # Initialize Arbitrum Web3 if needed
            if self.w3_arb is None:
                self.w3_arb = Web3(Web3.HTTPProvider(os.getenv('ARBITRUM_RPC_URL')))
            
            # Get ETH balances on both chains
            uni_eth_balance = await self.tycho_client.get_token_balance(self.eth_address)
            arb_eth_balance = self.w3_arb.eth.get_balance(os.getenv('PUBLIC_ADDRESS')) / 10**18
            
            logger.info("ETH balances", 
                       unichain=uni_eth_balance, 
                       arbitrum=arb_eth_balance, 
                       threshold=self.min_eth_balance)
            
            # Check if either chain needs gas
            if uni_eth_balance >= self.min_eth_balance and arb_eth_balance >= self.min_eth_balance:
                logger.info("ETH balance sufficient on both chains")
                return True
            
            # Calculate total ETH needed across both chains
            total_eth_needed = max(0, self.target_eth_balance - uni_eth_balance) + \
                              max(0, self.target_eth_balance - arb_eth_balance)
            
            # If we need more than 2x target, we'll swap for that amount on Unichain
            eth_to_acquire = max(2 * self.target_eth_balance - uni_eth_balance - arb_eth_balance, 0)
            
            logger.info("ETH refill calculation", 
                       total_needed=total_eth_needed,
                       to_acquire=eth_to_acquire)
            
            if eth_to_acquire > 0:
                # Get current ETH price
                eth_price = await self.perp_client.get_mark_price(self.asset)
                if eth_price <= 0:
                    logger.error("Invalid ETH price", price=eth_price)
                    return False
                
                # Calculate USD value needed
                usd_needed = eth_to_acquire * eth_price
                
                # Get balances on Unichain
                usdc_balance = await self.tycho_client.get_token_balance(self.asset_to_address_map['USDC'])
                asset_balance = await self.tycho_client.get_token_balance(self.asset_to_address_map[self.asset])
                
                # Calculate USD values
                usdc_value = usdc_balance  # USDC is 1:1 with USD
                asset_value = asset_balance * eth_price
                
                logger.info("Token balances for gas swap", 
                           usdc_balance=usdc_balance, usdc_value=usdc_value,
                           asset_balance=asset_balance, asset_value=asset_value,
                           usd_needed=usd_needed)
                
                # Choose source and swap to WETH
                swap_success = False
                if usdc_value >= usd_needed and usdc_value > asset_value:
                    # Use USDC
                    logger.info("Using USDC for gas refill", amount=usd_needed)
                    swap_success = await self._swap_and_unwrap(
                        from_token=self.asset_to_address_map['USDC'],
                        from_amount=usd_needed,
                        is_already_weth=False
                    )
                elif asset_value >= usd_needed:
                    # Use ASSET
                    if self.asset_to_address_map[self.asset].lower() == self.weth_address.lower():
                        # ASSET is already WETH, skip swap
                        logger.info("ASSET is WETH, skipping swap and unwrapping directly", amount=eth_to_acquire)
                        swap_success = await self.tycho_client.unwrap_weth(eth_to_acquire)
                    else:
                        # Need to swap ASSET to WETH first
                        logger.info("Using ASSET for gas refill", amount=eth_to_acquire)
                        swap_success = await self._swap_and_unwrap(
                            from_token=self.asset_to_address_map[self.asset],
                            from_amount=eth_to_acquire,
                            is_already_weth=False
                        )
                else:
                    logger.warning("Insufficient funds for gas refill", 
                                 usdc_value=usdc_value, asset_value=asset_value, needed=usd_needed)
                    return False
                
                if not swap_success:
                    logger.error("Failed to swap for ETH")
                    return False
            
            # Now handle cross-chain ETH balancing
            # Get updated balances after swap
            uni_eth_balance = await self.tycho_client.get_token_balance(self.eth_address)
            arb_eth_balance = self.w3_arb.eth.get_balance(os.getenv('PUBLIC_ADDRESS')) / 10**18
            
            # Determine which chain has more ETH and needs to send
            if arb_eth_balance > uni_eth_balance and uni_eth_balance < self.min_eth_balance:
                # Arbitrum has more, send to Unichain
                send_amount = min(arb_eth_balance - self.target_eth_balance, 
                                 self.target_eth_balance - uni_eth_balance)
                if send_amount > 0.001:  # Only bridge if amount is significant
                    logger.info("Bridging ETH from Arbitrum to Unichain via WETH", amount=send_amount)
                    
                    # Step 1: Wrap ETH to WETH on Arbitrum
                    wrap_success = await self._wrap_eth_arbitrum(send_amount)
                    if not wrap_success:
                        logger.error("Failed to wrap ETH to WETH on Arbitrum")
                        return False
                    
                    # Step 2: Bridge WETH from Arbitrum to Unichain
                    bridge_success = await self.bridge_client.bridge_weth_arbitrum_to_unichain(send_amount)
                    if not bridge_success:
                        logger.error("Failed to bridge WETH from Arbitrum to Unichain")
                        return False
                    
                    # Step 3: Unwrap WETH to ETH on Unichain
                    await asyncio.sleep(30)  # Wait for bridge to settle
                    unwrap_success = await self.tycho_client.unwrap_weth(send_amount)
                    if not unwrap_success:
                        logger.error("Failed to unwrap WETH on Unichain")
                        return False
                        
            elif uni_eth_balance > arb_eth_balance and arb_eth_balance < self.min_eth_balance:
                # Unichain has more, send to Arbitrum
                send_amount = min(uni_eth_balance - self.target_eth_balance,
                                 self.target_eth_balance - arb_eth_balance)
                if send_amount > 0.001:  # Only bridge if amount is significant
                    logger.info("Bridging ETH from Unichain to Arbitrum via WETH", amount=send_amount)
                    
                    # Step 1: Wrap ETH to WETH on Unichain
                    wrap_success = await self.tycho_client.wrap_eth(send_amount)
                    if not wrap_success:
                        logger.error("Failed to wrap ETH to WETH on Unichain")
                        return False
                    
                    # Step 2: Bridge WETH from Unichain to Arbitrum
                    bridge_success = await self.bridge_client.bridge_weth_unichain_to_arbitrum(send_amount)
                    if not bridge_success:
                        logger.error("Failed to bridge WETH from Unichain to Arbitrum")
                        return False
                    
                    # Step 3: Unwrap WETH to ETH on Arbitrum
                    await asyncio.sleep(30)  # Wait for bridge to settle
                    unwrap_success = await self._unwrap_weth_arbitrum(send_amount)
                    if not unwrap_success:
                        logger.error("Failed to unwrap WETH on Arbitrum")
                        return False
            
            # Verify final balances
            final_uni_eth = await self.tycho_client.get_token_balance(self.eth_address)
            final_arb_eth = self.w3_arb.eth.get_balance(os.getenv('PUBLIC_ADDRESS')) / 10**18
            
            logger.info("Gas refill completed", 
                       unichain_balance=final_uni_eth,
                       arbitrum_balance=final_arb_eth,
                       success=True)
            
            return True
                
        except Exception as e:
            logger.error("Error in gas check and refill", error=str(e))
            return False
    
    async def _swap_and_unwrap(self, from_token: str, from_amount: float, is_already_weth: bool) -> bool:
        """
        Helper function to swap tokens to WETH and then unwrap to ETH.
        """
        try:
            if not is_already_weth:
                # First swap to WETH
                swap_success = await self.tycho_client.swap_tokens(
                    from_token=from_token,
                    to_token=self.weth_address,
                    amount=from_amount
                )
                if not swap_success:
                    logger.error("Failed to swap to WETH", from_token=from_token, amount=from_amount)
                    return False
                
                # Wait a bit for the swap to settle
                await asyncio.sleep(2)
            
            # Get WETH balance to unwrap
            weth_balance = await self.tycho_client.get_token_balance(self.weth_address)
            if weth_balance <= 0:
                logger.error("No WETH balance to unwrap")
                return False
            
            # Unwrap WETH to ETH
            unwrap_success = await self.tycho_client.unwrap_weth(weth_balance)
            return unwrap_success
            
        except Exception as e:
            logger.error("Error in swap and unwrap", error=str(e))
            return False
    
    async def _wrap_eth_arbitrum(self, amount: float) -> bool:
        """
        Wrap ETH to WETH on Arbitrum.
        """
        try:
            # WETH contract on Arbitrum
            weth_address = '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1'
            with open('abis/WETH.json', 'r') as f:
                weth_abi = json.load(f)
            weth_contract = self.w3_arb.eth.contract(
                address=weth_address,
                abi=weth_abi
            )
            
            # Convert to Wei
            amount_wei = self.w3_arb.to_wei(amount, 'ether')
            
            # Check ETH balance first
            eth_balance = self.w3_arb.eth.get_balance(os.getenv('PUBLIC_ADDRESS'))
            estimated_gas_cost = 50000 * self.w3_arb.eth.gas_price  # Conservative estimate
            total_needed = amount_wei + estimated_gas_cost
            if eth_balance < total_needed:
                logger.error("Insufficient ETH balance on Arbitrum", 
                           have=self.w3_arb.from_wei(eth_balance, 'ether'), 
                           need=self.w3_arb.from_wei(total_needed, 'ether'))
                return False
            
            # Build transaction with dynamic gas estimation
            tx = weth_contract.functions.deposit().build_transaction({
                'from': os.getenv('PUBLIC_ADDRESS'),
                'value': amount_wei,
                'nonce': self.w3_arb.eth.get_transaction_count(os.getenv('PUBLIC_ADDRESS')),
                'gasPrice': self.w3_arb.eth.gas_price,
            })
            
            # Estimate gas dynamically
            try:
                estimated_gas = self.w3_arb.eth.estimate_gas(tx)
                tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
            except Exception as gas_error:
                logger.warning("Gas estimation failed, using fallback", error=str(gas_error))
                tx['gas'] = 50000  # Fallback
            
            # Sign and send
            signed_tx = self.w3_arb.eth.account.sign_transaction(tx, os.getenv('PRIVATE_KEY'))
            tx_hash = self.w3_arb.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            logger.info("ETH wrap transaction sent", tx_hash=tx_hash.hex(), amount=amount)
            
            # Wait for confirmation
            receipt = self.w3_arb.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                logger.info("Successfully wrapped ETH to WETH on Arbitrum", amount=amount, tx_hash=tx_hash.hex())
                return True
            else:
                logger.error("ETH wrap transaction failed", tx_hash=tx_hash.hex(), status=receipt['status'])
                return False
                
        except Exception as e:
            logger.error("Error wrapping ETH on Arbitrum", error=str(e))
            return False
    
    async def _unwrap_weth_arbitrum(self, amount: float) -> bool:
        """
        Unwrap WETH to ETH on Arbitrum.
        """
        try:
            # WETH contract on Arbitrum
            weth_address = '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1'
            with open('abis/WETH.json', 'r') as f:
                weth_abi = json.load(f)
            weth_contract = self.w3_arb.eth.contract(
                address=weth_address,
                abi=weth_abi
            )
            
            # Convert to Wei
            amount_wei = self.w3_arb.to_wei(amount, 'ether')
            
            # Check WETH balance first
            weth_balance = weth_contract.functions.balanceOf(os.getenv('PUBLIC_ADDRESS')).call()
            if weth_balance < amount_wei:
                logger.error("Insufficient WETH balance on Arbitrum", 
                           have=self.w3_arb.from_wei(weth_balance, 'ether'), need=amount)
                return False
            
            # Check ETH balance for gas
            eth_balance = self.w3_arb.eth.get_balance(os.getenv('PUBLIC_ADDRESS'))
            estimated_gas_cost = 100000 * self.w3_arb.eth.gas_price  # Conservative estimate
            if eth_balance < estimated_gas_cost:
                logger.error("Insufficient ETH for gas on Arbitrum", 
                           have=self.w3_arb.from_wei(eth_balance, 'ether'), 
                           needed_for_gas=self.w3_arb.from_wei(estimated_gas_cost, 'ether'))
                return False
            
            # Build transaction with dynamic gas estimation
            tx = weth_contract.functions.withdraw(amount_wei).build_transaction({
                'from': os.getenv('PUBLIC_ADDRESS'),
                'nonce': self.w3_arb.eth.get_transaction_count(os.getenv('PUBLIC_ADDRESS')),
                'gasPrice': self.w3_arb.eth.gas_price,
            })
            
            # Estimate gas dynamically
            try:
                estimated_gas = self.w3_arb.eth.estimate_gas(tx)
                tx['gas'] = int(estimated_gas * 1.2)  # Add 20% buffer
            except Exception as gas_error:
                logger.warning("Gas estimation failed, using fallback", error=str(gas_error))
                tx['gas'] = 100000  # Fallback
            
            # Sign and send
            signed_tx = self.w3_arb.eth.account.sign_transaction(tx, os.getenv('PRIVATE_KEY'))
            tx_hash = self.w3_arb.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            logger.info("WETH unwrap transaction sent", tx_hash=tx_hash.hex(), amount=amount)
            
            # Wait for confirmation
            receipt = self.w3_arb.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                logger.info("Successfully unwrapped WETH to ETH on Arbitrum", amount=amount, tx_hash=tx_hash.hex())
                return True
            else:
                logger.error("WETH unwrap transaction failed", tx_hash=tx_hash.hex(), status=receipt['status'])
                return False
                
        except Exception as e:
            logger.error("Error unwrapping WETH on Arbitrum", error=str(e))
            return False