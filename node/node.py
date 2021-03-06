import asyncio
import os
import random

from chain.dag import Dag
from chain.epoch import Epoch
from chain.signed_block import SignedBlock
from chain.block_factory import BlockFactory
from chain.params import Round, MINIMAL_SECRET_SHARERS, TOTAL_SECRET_SHARERS, ZETA
from chain.transaction_factory import TransactionFactory
from chain.conflict_watcher import ConflictWatcher
from node.behaviour import Behaviour
from node.block_signers import BlockSigner
from node.permissions import Permissions
from node.validators import Validators
from transaction.gossip_transaction import NegativeGossipTransaction, \
                                           PositiveGossipTransaction
from transaction.stake_transaction import PenaltyTransaction
from transaction.utxo import Utxo
from transaction.mempool import Mempool
from transaction.transaction_parser import TransactionParser
from verification.in_block_transactions_acceptor import InBlockTransactionsAcceptor
from verification.mempool_transactions_acceptor import MempoolTransactionsAcceptor
from verification.block_acceptor import BlockAcceptor, OrphanBlockAcceptor
from crypto.keys import Keys
from crypto.private import Private
from crypto.secret import split_secret, encode_splits
from hashlib import sha256


class DummyLogger(object):
    def __getattr__(self, name):
        return lambda *x: None


class Node:
    
    def __init__(self, genesis_creation_time, node_id, network,
                 block_signer=BlockSigner(Private.generate()),
                 validators=Validators(),
                 behaviour=Behaviour(),
                 logger=DummyLogger()):
        self.logger = logger
        self.dag = Dag(genesis_creation_time)
        self.epoch = Epoch(self.dag)
        self.epoch.set_logger(self.logger)
        self.permissions = Permissions(self.epoch, validators)
        self.mempool = Mempool()
        self.utxo = Utxo(self.logger)
        self.conflict_watcher = ConflictWatcher(self.dag)
        self.behaviour = behaviour

        self.block_signer = block_signer
        self.node_pubkey = Private.publickey(block_signer.private_key)
        self.logger.info("Public key is %s", Keys.to_visual_string(self.node_pubkey))
        self.network = network
        self.node_id = node_id
        self.epoch_private_keys = []  # TODO make this single element
        # self.epoch_private_keys where first element is era number, and second is key to reveal commited random
        self.reveals_to_send = {}
        self.sent_shares_epochs = []  # epoch hashes of secret shares
        self.last_expected_timeslot = 0
        self.last_signed_block_number = 0
        self.tried_to_sign_current_block = False
        self.owned_utxos = []
        self.terminated = False

        self.blocks_buffer = []  # uses while receive block and do not have its ancestor in local dag (before verify)

    def start(self):
        pass

    def handle_timeslot_changed(self, previous_timeslot_number, current_timeslot_number):
        self.last_expected_timeslot = current_timeslot_number
        self.try_to_broadcast_maliciously_delayed_block()
        return self.try_to_send_negative_gossip(previous_timeslot_number)
        
    def try_to_broadcast_maliciously_delayed_block(self):
        if self.behaviour.block_to_delay_broadcasting:
            if self.behaviour.malicious_block_broadcast_delay > 0:
                self.behaviour.malicious_block_broadcast_delay -= 1
            else:
                self.network.broadcast_block(self.node_id, self.behaviour.block_to_delay_broadcasting.pack())
                self.behaviour.block_to_delay_broadcasting = None

    def try_to_send_negative_gossip(self, previous_timeslot_number):
        if previous_timeslot_number not in self.dag.blocks_by_number:
            epoch_block_number = Epoch.convert_to_epoch_block_number(previous_timeslot_number)
            allowed_to_send_negative_gossip = False
            epoch_hashes = self.epoch.get_epoch_hashes()
            for _, epoch_hash in epoch_hashes.items():
                permissions = self.permissions.get_gossip_permission(epoch_hash, epoch_block_number)
                for permission in permissions:
                    if permission.public_key == self.node_pubkey:
                        allowed_to_send_negative_gossip = True
                        break
            if allowed_to_send_negative_gossip:
                self.broadcast_gossip_negative(previous_timeslot_number)
            return True
        return False

    def step(self):
        current_block_number = self.epoch.get_current_timeframe_block_number()

        if self.epoch.is_new_epoch_upcoming(current_block_number):
            self.epoch.accept_tops_as_epoch_hashes()

        # service method for update node behavior (if behavior is temporary)
        self.behaviour.update(Epoch.get_epoch_number(current_block_number))
        # service method for update transport behavior (if behavior is temporary)
        self.behaviour.update_transport(current_block_number)

        current_round = self.epoch.get_round_by_block_number(current_block_number)
        if current_round == Round.PUBLIC:
            self.try_to_publish_public_key(current_block_number)
        elif current_round == Round.SECRETSHARE:
            self.try_to_share_random()
            # elif current_round == Round.PRIVATE:
            # do nothing as private key should be included to block by block signer
        elif current_round == Round.COMMIT:
            self.try_to_commit_random()
        elif current_round == Round.REVEAL:
            self.try_to_reveal_random()
        elif current_round == Round.FINAL:
            # at this point we may remove everything systemic from mempool,
            # so it does not interfere with pubkeys for next epoch
            self.mempool.remove_all_systemic_transactions()

        if self.behaviour.wants_to_hold_stake:
            self.broadcast_stakehold_transaction()
            self.behaviour.wants_to_hold_stake = False

        if self.behaviour.wants_to_release_stake:
            self.broadcast_stakerelease_transaction()
            self.behaviour.wants_to_release_stake = False

        if self.behaviour.malicious_send_negative_gossip_count > 0:
            self.broadcast_gossip_negative(self.last_expected_timeslot)
            self.behaviour.malicious_send_negative_gossip_count -= 1
        if self.behaviour.malicious_send_positive_gossip_count > 0:
            zero_block = self.dag.blocks_by_number[0][0].block  # send genesis block malicious
            self.broadcast_gossip_positive(zero_block.get_hash())
            self.behaviour.malicious_send_positive_gossip_count -= 1

        if self.owned_utxos:
            self.broadcast_payments()

        if current_block_number != self.last_expected_timeslot:
            self.tried_to_sign_current_block = False
            should_wait = self.handle_timeslot_changed(previous_timeslot_number=self.last_expected_timeslot,
                                                       current_timeslot_number=current_block_number)
            if should_wait:
                return
        if not self.tried_to_sign_current_block:
            self.try_to_sign_block(current_block_number)
            self.tried_to_sign_current_block = True  # will reset in next timeslot

    async def run(self):
        while True:
            self.step()
            await asyncio.sleep(1)
    
    def try_to_sign_block(self, current_block_number):        
        epoch_block_number = Epoch.convert_to_epoch_block_number(current_block_number)
        
        allowed_to_sign = False
        epoch_hashes = self.epoch.get_epoch_hashes()
        for top, epoch_hash in epoch_hashes.items():
            permission = self.permissions.get_sign_permission(epoch_hash, epoch_block_number)
            if permission.public_key == self.node_pubkey:
                allowed_to_sign = True
                break

        if allowed_to_sign:
            should_skip_maliciously = self.behaviour.is_malicious_skip_block()
            # first_epoch_ever = self.epoch.get_epoch_number(current_block_number) == 1
            if should_skip_maliciously:  # and not first_epoch_ever: # skip first epoch check
                self.epoch_private_keys.clear()
                self.logger.info("Maliciously skiped block")
            else:
                if self.last_signed_block_number < current_block_number:
                    self.last_signed_block_number = current_block_number
                    self.sign_block(current_block_number)
                else:
                    # skip once more block broadcast in same timeslot
                    pass

    def sign_block(self, current_block_number):
        current_round_type = self.epoch.get_round_by_block_number(current_block_number)
        epoch_number = Epoch.get_epoch_number(current_block_number)
        
        system_txs = self.get_system_transactions_for_signing(current_round_type)
        payment_txs = self.get_payment_transactions_for_signing(current_block_number)

        tops = self.dag.get_top_blocks_hashes()
        chosen_top = self.dag.get_longest_chain_top(tops)
        conflicting_tops = [top for top in tops if top != chosen_top]

        current_top_blocks = [chosen_top] + conflicting_tops  # first link in dag is not considered conflict, the rest is.

        if self.behaviour.off_malicious_links_to_wrong_blocks:
            current_top_blocks = []
            all_hashes = list(self.dag.blocks_by_hash.keys())
            for _ in range(random.randint(1, 3)):
                block_hash = random.choice(all_hashes)
                current_top_blocks.append(block_hash)

            self.logger.info("Maliciously connecting block at slot %s to random hashes", current_block_number)

        block = BlockFactory.create_block_dummy(current_top_blocks)
        block.system_txs = system_txs
        block.payment_txs = payment_txs
        signed_block = BlockFactory.sign_block(block, self.block_signer.private_key)

        if self.behaviour.malicious_block_broadcast_delay > 0:
            self.behaviour.block_to_delay_broadcasting = signed_block
            return  # don't do broadcasting, wait a few timeslots1

        self.dag.add_signed_block(current_block_number, signed_block)
        self.utxo.apply_payments(payment_txs)
        self.conflict_watcher.on_new_block_by_validator(block.get_hash(), epoch_number, self.node_pubkey)

        if not self.behaviour.transport_cancel_block_broadcast:  # behaviour flag for cancel block broadcast
            self.logger.debug("Broadcasting signed block number %s", current_block_number)
            self.network.broadcast_block(self.node_id, signed_block.pack())
        else:
            self.logger.info("Created but maliciously skipped broadcasted block")

        if self.behaviour.malicious_excessive_block_count > 0:
            additional_block_timestamp = block.timestamp + 1
            additional_block = BlockFactory.create_block_with_timestamp(current_top_blocks, additional_block_timestamp)
            additional_block.system_txs = block.system_txs
            additional_block.payment_txs = block.payment_txs
            signed_add_block = BlockFactory.sign_block(additional_block, self.block_signer.private_key)
            self.dag.add_signed_block(current_block_number, signed_add_block)
            self.conflict_watcher.on_new_block_by_validator(signed_add_block.get_hash(), epoch_number, self.node_pubkey) #mark our own conflict for consistency
            self.logger.info("Sending additional block")
            self.network.broadcast_block(self.node_id, signed_add_block.pack())
            self.behaviour.malicious_excessive_block_count -= 1

    def get_system_transactions_for_signing(self, round):
        system_txs = self.mempool.pop_round_system_transactions(round)

        # skip non valid system_txs
        verifier = InBlockTransactionsAcceptor(self.epoch, self.permissions, self.logger)
        system_txs = [t for t in system_txs if verifier.check_if_valid(t)]
        # get gossip conflicts hashes (validate_gossip() ---> [gossip_negative_hash, gossip_positive_hash])
        conflicts_gossip = self.validate_gossip(self.dag, self.mempool)
        gossip_mempool_txs = self.mempool.pop_current_gossips()  # POP gossips to block
        system_txs += gossip_mempool_txs

        if round == Round.PRIVATE:
            if self.epoch_private_keys:
                key_reveal_tx = self.form_private_key_reveal_transaction()
                system_txs.append(key_reveal_tx)

        if conflicts_gossip:
            for conflict in conflicts_gossip:
                self.logger.info("Adding penalty to block with conflicting gossips %s", conflicts_gossip)
                penalty_gossip_tx = \
                    TransactionFactory.create_penalty_gossip_transaction(conflict=conflict,
                                                                         node_private=self.block_signer.private_key)
                system_txs.append(penalty_gossip_tx)
        
        return system_txs

    def get_payment_transactions_for_signing(self, block_number):
        node_public = Private.publickey(self.block_signer.private_key)
        pseudo_address = sha256(node_public).digest()
        block_reward = TransactionFactory.create_block_reward(pseudo_address, block_number)
        block_reward_hash = block_reward.get_hash()
        self.owned_utxos.append(block_reward_hash)
        payment_txs = [block_reward] + self.mempool.pop_payment_transactions()
        return payment_txs

    def try_to_publish_public_key(self, current_block_number):
        if self.epoch_private_keys:
            return
        
        epoch_hashes = self.epoch.get_epoch_hashes()
        for _, epoch_hash in epoch_hashes.items():
            allowed_round_validators = self.permissions.get_ordered_randomizers_pubkeys_for_round(epoch_hash, Round.PUBLIC)
            pubkey_publishers_pubkeys = [validator.public_key for validator in allowed_round_validators]
            if self.node_pubkey in pubkey_publishers_pubkeys:
                node_private = self.block_signer.private_key
                pubkey_index = self.permissions.get_signer_index_from_public_key(self.node_pubkey, epoch_hash)

                generated_private = Private.generate()
                tx = TransactionFactory.create_public_key_transaction(generated_private=generated_private,
                                                                      epoch_hash=epoch_hash,
                                                                      validator_index=pubkey_index,
                                                                      node_private=node_private)
                if self.behaviour.malicious_wrong_signature:
                    tx.signature = b'0' + tx.signature[1:]
                    
                self.epoch_private_keys.append(generated_private)
                self.logger.debug("Broadcasted public key")
                self.logger.debug(Keys.to_visual_string(tx.generated_pubkey))
                self.mempool.add_transaction(tx)
                self.network.broadcast_transaction(self.node_id, TransactionParser.pack(tx))
    
    def try_to_share_random(self):
        epoch_hashes = self.epoch.get_epoch_hashes()
        for top, epoch_hash in epoch_hashes.items():
            if epoch_hash in self.sent_shares_epochs: continue
            allowed_to_share_random = self.permissions.get_secret_sharers_pubkeys(epoch_hash)
            if not self.node_pubkey in allowed_to_share_random: continue
            split_random = self.form_split_random_transaction(top, epoch_hash)
            self.sent_shares_epochs.append(epoch_hash)
            self.mempool.add_transaction(split_random)
            self.network.broadcast_transaction(self.node_id, TransactionParser.pack(split_random))

    def try_to_commit_random(self):
        epoch_hashes = self.epoch.get_epoch_hashes().values()
        for epoch_hash in epoch_hashes:
            if epoch_hash not in self.reveals_to_send:
                allowed_to_commit_list = self.permissions.get_commiters_pubkeys(epoch_hash)
                if self.node_pubkey not in allowed_to_commit_list:
                    continue
                pubkey_index = self.permissions.get_committer_index_from_public_key(self.node_pubkey, epoch_hash)
                commit, reveal = TransactionFactory.create_commit_reveal_pair(self.block_signer.private_key, os.urandom(32), pubkey_index, epoch_hash)
                self.reveals_to_send[epoch_hash] = reveal
                self.logger.info("Broadcasting commit")
                self.mempool.add_transaction(commit)
                self.network.broadcast_transaction(self.node_id, TransactionParser.pack(commit))
    
    def try_to_reveal_random(self):
        for epoch_hash in list(self.reveals_to_send.keys()):
            reveal = self.reveals_to_send[epoch_hash]
            self.logger.info("Broadcasting reveal")
            self.mempool.add_transaction(reveal)
            self.network.broadcast_transaction(self.node_id, TransactionParser.pack(reveal))
            del self.reveals_to_send[epoch_hash]

    def form_private_key_reveal_transaction(self):
        tx = TransactionFactory.create_private_key_transaction(self.epoch_private_keys.pop(0))
        return tx

    def form_split_random_transaction(self, top_hash, epoch_hash):
        ordered_senders = self.permissions.get_ordered_randomizers_pubkeys_for_round(epoch_hash, Round.PUBLIC)
        published_pubkeys = self.epoch.get_public_keys_for_epoch(top_hash)
        
        self.logger.info("Ordered pubkeys for secret sharing:")
        sorted_published_pubkeys = []
        for sender in ordered_senders:
            raw_pubkey = Keys.to_bytes(sender.public_key)
            raw_pubkey_index = self.permissions.get_signer_index_from_public_key(raw_pubkey, epoch_hash)
            if raw_pubkey_index in published_pubkeys:
                generated_pubkey = published_pubkeys[raw_pubkey_index]
                sorted_published_pubkeys.append(Keys.from_bytes(generated_pubkey))
                self.logger.info(Keys.to_visual_string(generated_pubkey))
            else:
                sorted_published_pubkeys.append(None)
                self.logger.info("None")

        tx = self.form_secret_sharing_transaction(sorted_published_pubkeys, epoch_hash)
        return tx

    def form_secret_sharing_transaction(self, sorted_public_keys, epoch_hash):
        random_bytes = os.urandom(32)
        splits = split_secret(random_bytes, MINIMAL_SECRET_SHARERS, TOTAL_SECRET_SHARERS)
        encoded_splits = encode_splits(splits, sorted_public_keys)
        self.logger.info("Formed split random")

        node_private = self.block_signer.private_key
        pubkey_index = self.permissions.get_secret_sharer_from_public_key(self.node_pubkey, epoch_hash)

        tx = TransactionFactory.create_split_random_transaction(encoded_splits, pubkey_index, epoch_hash, node_private)
        return tx

    def get_allowed_signers_for_next_block(self, block):
        current_block_number = self.epoch.get_current_timeframe_block_number()
        epoch_block_number = Epoch.convert_to_epoch_block_number(current_block_number)
        if self.epoch.is_new_epoch_upcoming(current_block_number):
            self.epoch.accept_tops_as_epoch_hashes()
        epoch_hashes = self.epoch.get_epoch_hashes()
        allowed_signers = []
        for prev_hash in block.prev_hashes:
            epoch_hash = None
            if prev_hash in epoch_hashes:
                epoch_hash = epoch_hashes[prev_hash]
            else:
                epoch_hash = self.epoch.find_epoch_hash_for_block(prev_hash)
            
            if epoch_hash:
                # self.logger.info("Calculating permissions from epoch_hash %s", epoch_hash.hex())
                allowed_pubkey = self.permissions.get_sign_permission(epoch_hash, epoch_block_number)
                allowed_signers.append(allowed_pubkey)

        assert len(allowed_signers) > 0, "No signers allowed to sign next block"
        return allowed_signers

    # -------------------------------------------------------------------------------
    # Handlers
    # -------------------------------------------------------------------------------
    def handle_block_message(self, node_id, raw_signed_block):
        signed_block = SignedBlock()
        signed_block.parse(raw_signed_block)
        block_number = self.epoch.get_block_number_from_timestamp(signed_block.block.timestamp)
        self.logger.info("Received block with number %s at timeslot %s with hash %s", block_number, self.epoch.get_current_timeframe_block_number(), signed_block.block.get_hash().hex())


        # CHECK_ANCESTOR
        blocks_by_hash = self.dag.blocks_by_hash
        is_orphan_block = False
        for prev_hash in signed_block.block.prev_hashes:  # by every previous hash
            if prev_hash not in blocks_by_hash:  # verify local ancestor for incoming block
                is_orphan_block = True

        # CHECK_ORPHAN_DISTANCE
        block_out_of_epoch = False
        epoch_end_block = self.epoch.get_epoch_end_block_number(self.epoch.current_epoch)
        if block_number >= epoch_end_block:
            # income block from future epoch, cant validate signer
            block_out_of_epoch = True

        # CHECK ALLOWED SIGNER
        if not block_out_of_epoch:  # if incoming block not out of current epoch
            allowed_signers = self.get_allowed_signers_for_block_number(block_number)
            allowed_pubkey = None
            for allowed_signer in allowed_signers:
                if signed_block.verify_signature(allowed_signer):
                    allowed_pubkey = allowed_signer
                    break
        else:
            allowed_pubkey = 'block_out_of_epoch'  # process block as orphan

        if allowed_pubkey:  # IF SIGNER ALLOWED
            if not is_orphan_block:  # PROCESS NORMAL BLOCK (same epoch)
                if self.epoch.is_new_epoch_upcoming(block_number):  # CHECK IS NEW EPOCH
                    self.epoch.accept_tops_as_epoch_hashes()
                block_verifier = BlockAcceptor(self.epoch, self.logger)  # VERIFY BLOCK AS NORMAL
                if block_verifier.check_if_valid(signed_block.block):
                    self.insert_verified_block(signed_block, allowed_pubkey)
                    return
            else:  # PROCESS ORPHAN BLOCK (same epoch)
                orphan_block_verifier = OrphanBlockAcceptor(self.epoch, self.blocks_buffer, self.logger)
                if orphan_block_verifier.check_if_valid(signed_block.block):
                    self.blocks_buffer.append(signed_block)
                    self.logger.info("Orphan block added to buffer")
                    # for every parent for received block
                    for prev_hash in signed_block.block.prev_hashes:  # check received block ancestor
                        if prev_hash not in self.dag.blocks_by_hash:  # check parent in local dag
                            # if parent not exist in local DAG ask for parent
                            self.network.direct_request_block_by_hash(self.node_id, node_id, prev_hash)

                if len(self.blocks_buffer) > 0:
                    self.process_block_buffer()
                    self.logger.info("Orphan block buffer process success")
        else:
            self.logger.error("Received block from %d, but its signature is wrong", node_id)

    def handle_transaction_message(self, node_id, raw_transaction):
        transaction = TransactionParser.parse(raw_transaction)

        verifier = MempoolTransactionsAcceptor(self.epoch, self.permissions, self.logger)
        if verifier.check_if_valid(transaction):
            self.mempool.add_transaction(transaction)
            # PROCESS NEGATIVE GOSSIP
            if isinstance(transaction, NegativeGossipTransaction):
                self.logger.info("Received negative gossip about block %s at timeslot %s", transaction.number_of_block,self.epoch.get_current_timeframe_block_number())

                current_gossips = self.mempool.get_negative_gossips_by_block(transaction.number_of_block)
                for gossip in current_gossips:
                    # negative gossip already send by node, skip positive gossip searching and broadcasting
                    if gossip.pubkey == self.node_pubkey:
                        return
                if self.dag.has_block_number(transaction.number_of_block):
                    signed_block_by_number = self.dag.blocks_by_number[transaction.number_of_block]
                    self.broadcast_gossip_positive(signed_block_by_number[0].get_hash())
            # PROCESS POSITIVE GOSSIP
            if isinstance(transaction, PositiveGossipTransaction):
                # ----> ! make request ONLY if block in timeslot
                self.logger.info("Received positive gossip about block %s at timeslot %s", transaction.block_hash.hex(),self.epoch.get_current_timeframe_block_number())
                if transaction.block_hash not in self.dag.blocks_by_hash:
                    self.network.get_block_by_hash(sender_node_id=self.node_id,
                                                   receiver_node_id=node_id,  # request TO ----> receiver_node_id
                                                   block_hash=transaction.block_hash)
        else:
            self.logger.error("Received tx is invalid")

    # -------------------------------------------------------------------------------
    # Broadcast
    # -------------------------------------------------------------------------------
    def broadcast_stakehold_transaction(self):
        node_private = self.block_signer.private_key
        tx = TransactionFactory.create_stake_hold_transaction(1000, node_private)
        self.logger.info("Broadcasted StakeHold transaction")
        self.network.broadcast_transaction(self.node_id, TransactionParser.pack(tx))

    def broadcast_stakerelease_transaction(self):
        node_private = self.block_signer.private_key
        tx = TransactionFactory.create_stake_release_transaction(node_private)
        self.logger.info("Broadcasted release stake transaction")
        self.network.broadcast_transaction(self.node_id, TransactionParser.pack(tx))

    def broadcast_gossip_negative(self, block_number):
        node_private = self.block_signer.private_key
        tx = TransactionFactory.create_negative_gossip_transaction(block_number, node_private)
        self.mempool.append_gossip_tx(tx)  # ADD ! TO LOCAL MEMPOOL BEFORE BROADCAST
        self.logger.info("Broadcasted negative gossip transaction")
        self.network.broadcast_transaction(self.node_id, TransactionParser.pack(tx))

    def broadcast_gossip_positive(self, signed_block_hash):
        node_private = self.block_signer.private_key
        tx = TransactionFactory.create_positive_gossip_transaction(signed_block_hash, node_private)
        self.mempool.append_gossip_tx(tx)  # ADD ! TO LOCAL MEMPOOL BEFORE BROADCAST
        # self.logger.info("Broadcasted positive gossip transaction")
        self.network.broadcast_transaction(self.node_id, TransactionParser.pack(tx))

    def broadcast_payments(self):
        for utxo in self.owned_utxos:
            tx = TransactionFactory.create_payment(utxo, 0, [os.urandom(32), os.urandom(32)], [10, 5])
            self.mempool.add_transaction(tx)
            self.network.broadcast_transaction(self.node_id, TransactionParser.pack(tx))
            # self.logger.info("Broadcasted payment with hash %s", tx.get_hash())
        self.owned_utxos.clear()

    # -------------------------------------------------------------------------------
    # Targeted request
    # -------------------------------------------------------------------------------
    def request_block_by_hash(self, block_hash):
        # no need validate/ public info ?
        signed_block = self.dag.blocks_by_hash[block_hash]
        self.network.broadcast_block(self.node_id, signed_block.pack())

    # method returns block directly to sender without broadcast
    def direct_request_block_by_hash(self, sender_node, block_hash):
        signed_block = self.dag.blocks_by_hash[block_hash]
        self.network.direct_response_block_by_hash(self.node_id, sender_node, signed_block.pack())

    # -------------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------------
    def insert_verified_block(self, signed_block, allowed_pubkey):
        block = signed_block.block
        block_number = self.epoch.get_block_number_from_timestamp(block.timestamp)
        epoch_number = Epoch.get_epoch_number(block_number)

        self.dag.add_signed_block(block_number, signed_block)
        self.mempool.remove_transactions(block.system_txs)
        self.mempool.remove_transactions(block.payment_txs)
        self.utxo.apply_payments(block.payment_txs)
        self.conflict_watcher.on_new_block_by_validator(block.get_hash(), epoch_number, allowed_pubkey)

    def process_block_buffer(self):
        while len(self.blocks_buffer) > 0:
            block_from_buffer = self.blocks_buffer.pop()
            block_number = self.epoch.get_block_number_from_timestamp(block_from_buffer.block.timestamp)

            if self.epoch.is_new_epoch_upcoming(block_number):  # CHECK IS NEW EPOCH
                self.epoch.accept_tops_as_epoch_hashes()

            # validate block from buffer by signature
            allowed_signers = self.get_allowed_signers_for_block_number(block_number)
            allowed_pubkey = None
            for allowed_signer in allowed_signers:
                if block_from_buffer.verify_signature(allowed_signer):
                    allowed_pubkey = allowed_signer
                    break

            if allowed_pubkey:
                block_verifier = BlockAcceptor(self.epoch, self.logger)
                if block_verifier.check_if_valid(block_from_buffer.block):  # VERIFY BLOCK AS NORMAL
                    self.insert_verified_block(block_from_buffer, allowed_pubkey)
                else:
                    self.logger.info("Block from buffer verification failed")
            else:
                self.logger.info("Block from buffer wrong signature")

    def get_allowed_signers_for_block_number(self, block_number):
        # TODO take cached epoch hashes if block is of lastest epoch
        prev_epoch_number = self.epoch.get_epoch_number(block_number) - 1
        prev_epoch_start = self.epoch.get_epoch_start_block_number(prev_epoch_number)
        prev_epoch_end = self.epoch.get_epoch_end_block_number(prev_epoch_number)
        
        # this will extract every unconnected block in epoch, which is practically epoch hash
        # TODO maybe consider blocks to be epoch hashes if they are in final round and consider everything else is orphan
        epoch_hashes = self.dag.get_branches_for_timeslot_range(prev_epoch_start, prev_epoch_end + 1)
        
        if prev_epoch_number == 0:
            epoch_hashes = [self.dag.genesis_block().get_hash()]

        allowed_signers = []
        for epoch_hash in epoch_hashes:
            epoch_block_number = Epoch.convert_to_epoch_block_number(block_number)
            allowed_pubkey = self.permissions.get_sign_permission(epoch_hash, epoch_block_number).public_key
            allowed_signers.append(allowed_pubkey)

        assert len(allowed_signers) > 0, "No signers allowed to sign block"
        return allowed_signers

    @staticmethod
    def validate_gossip(dag, mempool):
        result = []

        # -------------- mempool validation
        mem_negative_gossips = mempool.get_all_negative_gossips()
        # for every negative in mempool get authors and positives
        for negative in mem_negative_gossips:  # we can have many negatives by not existing block
            negative_author = negative.pubkey
            # get block by negative number
            # skip another validations (if current validator have no block)
            if dag.has_block_number(negative.number_of_block):
                # get block hash
                blocks_by_negative = dag.blocks_by_number[negative.number_of_block]
                for block in blocks_by_negative:  # we can have more than one block by number
                    positives_for_negative = \
                        mempool.get_positive_gossips_by_block_hash(block.get_hash())
                    # if have no positives for negative - do nothing
                    for positive in positives_for_negative:
                        if positive.pubkey == negative_author:
                            # add to conflict result positive and negative gossips hash with same author
                            result.append([positive.get_hash(), negative.get_hash()])

        # -------------- dag validation
        # provide penalty for standalone positive gossip (without negative) ?
        # what else we can validate by tx_by_hash ?
        # dag_negative_gossips = dag.get_negative_gossips()
        # dag_positive_gossips = dag.get_positive_gossips()
        # dag_penalty_gossips = dag.get_penalty_gossips()
        return result

