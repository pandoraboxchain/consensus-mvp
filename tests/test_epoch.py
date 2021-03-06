import unittest
import os

from chain.block import Block
from chain.dag import Dag
from chain.epoch import Epoch, RoundIter, BLOCK_TIME
from chain.params import Round
from chain.block_factory import BlockFactory
from transaction.secret_sharing_transactions import PublicKeyTransaction, PrivateKeyTransaction, SplitRandomTransaction
from transaction.commit_transactions import CommitRandomTransaction, RevealRandomTransaction
from crypto.sum_random import sum_random
from crypto.private import Private
from crypto.public import Public
from crypto.secret import split_secret, encode_splits, decode_random
from crypto.keys import Keys
from chain.params import ROUND_DURATION

from tools.chain_generator import ChainGenerator


class TestEpoch(unittest.TestCase):

    def test_secret_sharing_rounds(self):
        dag = Dag(0)
        epoch = Epoch(dag)

        dummy_private = Private.generate()

        signers = []
        for i in range(0, ROUND_DURATION + 1):
            signers.append(Private.generate())

        private_keys = []

        block_number = 1
        genesis_hash = dag.genesis_block().get_hash()
        prev_hash = genesis_hash
        signer_index = 0
        for i in Epoch.get_round_range(1, Round.PUBLIC):
            private = Private.generate()
            private_keys.append(private)

            signer = signers[signer_index]
            pubkey_tx = PublicKeyTransaction()
            pubkey_tx.generated_pubkey = Private.publickey(private)
            pubkey_tx.pubkey_index = signer_index
            pubkey_tx.signature = Private.sign(pubkey_tx.get_signing_hash(genesis_hash), signer)

            block = Block()
            block.timestamp = i * BLOCK_TIME
            block.prev_hashes = [prev_hash]
            block.system_txs = [pubkey_tx]
            signed_block = BlockFactory.sign_block(block, signer)
            dag.add_signed_block(i, signed_block)
            signer_index += 1
            prev_hash = block.get_hash()

        prev_hash = ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.COMMIT))

        public_keys = []
        for private in private_keys:
            public_keys.append(Private.publickey(private))

        randoms_list = []
        expected_random_pieces = []
        for i in Epoch.get_round_range(1, Round.SECRETSHARE):
            random_bytes = os.urandom(32)
            random_value = int.from_bytes(random_bytes, byteorder='big')
            split_random_tx = SplitRandomTransaction()
            splits = split_secret(random_bytes, 2, 3)
            encoded_splits = encode_splits(splits, public_keys)
            split_random_tx.pieces = encoded_splits
            split_random_tx.pubkey_index = 0
            expected_random_pieces.append(split_random_tx.pieces)
            split_random_tx.signature = Private.sign(pubkey_tx.get_hash(), dummy_private)
            block = Block()
            block.timestamp = i * BLOCK_TIME
            block.prev_hashes = [prev_hash]
            block.system_txs = [split_random_tx]
            signed_block = BlockFactory.sign_block(block, dummy_private)
            dag.add_signed_block(i, signed_block)
            randoms_list.append(random_value)
            prev_hash = block.get_hash()

        expected_seed = sum_random(randoms_list)  

        prev_hash = ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.REVEAL))

        signer_index = 0
        private_key_index = 0
        raw_private_keys = []
        for i in Epoch.get_round_range(1, Round.PRIVATE):
            private_key_tx = PrivateKeyTransaction()
            private_key_tx.key = Keys.to_bytes(private_keys[private_key_index])
            raw_private_keys.append(private_key_tx.key)
            signer = signers[signer_index]
            block = Block()
            block.system_txs = [private_key_tx]
            block.prev_hashes = [prev_hash]
            block.timestamp = block_number * BLOCK_TIME            
            signed_block = BlockFactory.sign_block(block, signer)
            dag.add_signed_block(i, signed_block)          
            signer_index += 1
            private_key_index += 1
            prev_hash = block.get_hash()
        
        prev_hash = ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.FINAL))

        top_block_hash = dag.get_top_blocks_hashes()[0]

        random_splits = epoch.get_random_splits_for_epoch(top_block_hash)
        self.assertEqual(expected_random_pieces, random_splits)

        restored_randoms = []
        for i in range(0, len(random_splits)):
            random = decode_random(random_splits[i], Keys.list_from_bytes(raw_private_keys))
            restored_randoms.append(random)

        self.assertEqual(randoms_list, restored_randoms)

        seed = epoch.extract_shared_random(top_block_hash)
        self.assertEqual(expected_seed, seed)

    def test_commit_reveal(self):
        dag = Dag(0)
        epoch = Epoch(dag)

        private = Private.generate()

        prev_hash = ChainGenerator.fill_with_dummies(dag, dag.genesis_block().get_hash(), Epoch.get_round_range(1, Round.PUBLIC))

        randoms_list = []
        for i in Epoch.get_round_range(1, Round.COMMIT):
            random_value = int.from_bytes(os.urandom(32), byteorder='big')
            randoms_list.append(random_value)

        expected_seed = sum_random(randoms_list)

        reveals = []

        epoch_hash = dag.genesis_block().get_hash()

        for i in Epoch.get_round_range(1, Round.COMMIT):
            rand = randoms_list.pop()
            random_bytes = rand.to_bytes(32, byteorder='big')
            commit, reveal = TestEpoch.create_dummy_commit_reveal(random_bytes, epoch_hash)
            commit_block = BlockFactory.create_block_with_timestamp([prev_hash], i * BLOCK_TIME)
            commit_block.system_txs = [commit]
            signed_block = BlockFactory.sign_block(commit_block, private)
            dag.add_signed_block(i, signed_block)
            prev_hash = commit_block.get_hash()
            reveals.append(reveal)

            revealing_key = Keys.from_bytes(reveal.key)
            encrypted_bytes = Public.encrypt(random_bytes, Private.publickey(revealing_key))
            decrypted_bytes = Private.decrypt(encrypted_bytes, revealing_key)
            # TODO check if encryption decryption can work million times in a row
            self.assertEqual(decrypted_bytes, random_bytes)

            revealed_value = Private.decrypt(commit.rand, revealing_key)
            self.assertEqual(revealed_value, random_bytes)

        # self.assertEqual(len(reveals), ROUND_DURATION)

        prev_hash = ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.SECRETSHARE))

        for i in Epoch.get_round_range(1, Round.REVEAL):
            reveal_block = BlockFactory.create_block_with_timestamp([prev_hash], i * BLOCK_TIME)
            reveal_block.system_txs = [reveals.pop()]
            signed_block = BlockFactory.sign_block(reveal_block, private)
            dag.add_signed_block(i, signed_block)
            prev_hash = reveal_block.get_hash()           

        prev_hash = ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.PRIVATE))

        prev_hash = ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.FINAL))

        seed = epoch.reveal_commited_random(prev_hash)
        self.assertEqual(expected_seed, seed)

    def test_top_blocks(self):
        dag = Dag(0)
        epoch = Epoch(dag)
        private = Private.generate()
        
        epoch_hash = dag.genesis_block().get_hash()
        
        self.assertEqual(dag.genesis_block().get_hash(), list(epoch.get_epoch_hashes().keys())[0])
        self.assertEqual(dag.genesis_block().get_hash(), list(epoch.get_epoch_hashes().values())[0])

        block1 = BlockFactory.create_block_with_timestamp([dag.genesis_block().get_hash()], BLOCK_TIME)
        signed_block1 = BlockFactory.sign_block(block1, private)
        dag.add_signed_block(1, signed_block1)

        self.assertEqual(block1.get_hash(), list(epoch.get_epoch_hashes().keys())[0])
        self.assertEqual(epoch_hash, list(epoch.get_epoch_hashes().values())[0])

        prev_hash = block1.get_hash()
        epoch_length = ROUND_DURATION * 6 + 1
        for i in range(2, epoch_length + 1):
            block = BlockFactory.create_block_with_timestamp([prev_hash], BLOCK_TIME * i)
            signed_block = BlockFactory.sign_block(block, private)
            dag.add_signed_block(i, signed_block)
            prev_hash = block.get_hash()

        if epoch.is_new_epoch_upcoming(epoch_length + 1):
            epoch.accept_tops_as_epoch_hashes()

        top_block_hash = dag.blocks_by_number[epoch_length][0].get_hash()
        epoch_hash = dag.blocks_by_number[epoch_length][0].get_hash()

        self.assertEqual(top_block_hash, list(epoch.get_epoch_hashes().keys())[0])
        self.assertEqual(epoch_hash, list(epoch.get_epoch_hashes().values())[0])

        epoch2 = epoch_length*2+1
        for i in range(epoch_length + 1, epoch2):
            block = BlockFactory.create_block_with_timestamp([prev_hash], BLOCK_TIME * i)
            signed_block = BlockFactory.sign_block(block, private)
            dag.add_signed_block(i, signed_block)
            prev_hash = block.get_hash()

        if epoch.is_new_epoch_upcoming(epoch2):
            epoch.accept_tops_as_epoch_hashes()

        top_block_hash = dag.blocks_by_number[epoch2-1][0].get_hash()
        epoch_hash = dag.blocks_by_number[epoch2-1][0].get_hash()

        self.assertEqual(top_block_hash, list(epoch.get_epoch_hashes().keys())[0])
        self.assertEqual(epoch_hash, list(epoch.get_epoch_hashes().values())[0])

    def test_private_keys_extraction(self):
        dag = Dag(0)
        epoch = Epoch(dag)
        node_private = Private.generate()

        prev_hash = dag.genesis_block().get_hash()
        round_start, round_end = Epoch.get_round_bounds(1, Round.PRIVATE)
        for i in range(1, round_start):
            block = BlockFactory.create_block_with_timestamp([prev_hash], BLOCK_TIME * i)
            signed_block = BlockFactory.sign_block(block, node_private)
            dag.add_signed_block(i, signed_block)
            prev_hash = block.get_hash()

        generated_private_keys = []
        for i in range(round_start, round_end):  # intentionally skip last block of round
            generated_private = Private.generate()
            generated_private_keys.append(Keys.to_bytes(generated_private))

            private_key_tx = PrivateKeyTransaction()
            private_key_tx.key = Keys.to_bytes(generated_private)
            block = Block()
            block.system_txs = [private_key_tx]
            block.prev_hashes = dag.get_top_blocks_hashes()
            block.timestamp = i * BLOCK_TIME            
            signed_block = BlockFactory.sign_block(block, node_private)
            dag.add_signed_block(i, signed_block)
            prev_hash = block.get_hash()

        ChainGenerator.fill_with_dummies(dag, prev_hash, Epoch.get_round_range(1, Round.FINAL))

        epoch_hash = dag.blocks_by_number[ROUND_DURATION * 6 + 1][0].get_hash()

        extracted_privates = epoch.get_private_keys_for_epoch(epoch_hash)

        for i in range(0, ROUND_DURATION - 1):
            self.assertEqual(extracted_privates[i], generated_private_keys[i])

    # TODO use method for creating commit-reveal pair from Node.py
    @staticmethod
    def create_dummy_commit_reveal(random_bytes, epoch_hash):
        node_private = Private.generate()

        private = Private.generate()

        encoded = Private.encrypt(random_bytes, private)

        commit = CommitRandomTransaction()
        commit.rand = encoded
        commit.pubkey_index = 10
        commit.signature = Private.sign(commit.get_signing_hash(epoch_hash), node_private)

        reveal = RevealRandomTransaction()
        reveal.commit_hash = commit.get_hash()
        reveal.key = Keys.to_bytes(private)

        return commit, reveal
    
    def test_find_epoch_hash_for_block(self):
        dag = Dag(0)
        epoch = Epoch(dag)
        genesis_hash = dag.genesis_block().get_hash()
        genesis_epoch_hash = epoch.find_epoch_hash_for_block(genesis_hash)
        self.assertEqual(genesis_hash, genesis_epoch_hash)

        block = BlockFactory.create_block_with_timestamp([genesis_hash], BLOCK_TIME)
        signed_block = BlockFactory.sign_block(block, Private.generate())
        dag.add_signed_block(1, signed_block)
        first_block_hash = block.get_hash()

        first_epoch_hash = epoch.find_epoch_hash_for_block(first_block_hash)
        self.assertEqual(genesis_hash, first_epoch_hash)