from chain.block import Block
from transaction.transaction import CommitRandomTransaction, RevealRandomTransaction
from crypto.dec_part_random import dec_part_random

class CommitsSet:
    
    transactions_by_hash = {}
    transactions_by_pubkey = {}

    def __init__(self, dag, top_block_hash):
        self.dag = dag
        self.recursive_collect_commited_transactions(top_block_hash)

    def recursive_collect_commited_transactions(self, tx_hash):
        block = self.dag.blocks_by_hash[tx_hash]
        if not hasattr(block, "system_txs"):
            return
        for tx in block.system_txs:
            if isinstance(tx, CommitRandomTransaction):
                self.add_transaction(tx)

        for prev_hash in block.prev_hashes:
            self.recursive_collect_commited_transactions(prev_hash)
        

    def add_transaction(self, tx):
        tx_hash = tx.get_hash().digest()
        self.transactions_by_hash[tx_hash] = tx

        pubkey = tx.pubkey
        self.transactions_by_pubkey[pubkey] = tx

