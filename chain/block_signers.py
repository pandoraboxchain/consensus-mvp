from chain.block_signer import BlockSigner
from base64 import b64decode,b64encode
from Crypto.PublicKey import RSA

class BlockSigners():

    def __init__(self):
        self.block_signers = []
        self.get_from_file()

    def get_from_file(self):
        with open('keys') as f:
            lines = f.readlines()

        for line in lines:
            decode = b64decode(line)
            if len(decode)!=0:
                key = RSA.importKey(decode)
                block_signer = BlockSigner(key)
                self.block_signers.append(block_signer)
