from serialization.serializer import Serializer, Deserializer
from Crypto.Hash import SHA256

class CommitRandomTransaction():
    def parse(self, raw_data):
        deserializer = Deserializer(raw_data)
        self.rand = deserializer.parse_encrypted_data()
        self.pubkey = deserializer.parse_pubkey()
        self.signature = deserializer.parse_signature()
        self.len = deserializer.get_len()
    
    def pack(self):
        return  Serializer.write_encrypted_data(self.rand) + \
                self.pubkey + \
                Serializer.write_signature(self.signature)
    
    def get_len(self):
        return self.len

    #this hash includes epoch_hash for checking if random wasn't reused
    def get_signing_hash(self, epoch_hash):
        return SHA256.new(self.rand + self.pubkey + epoch_hash).digest()
    
    #this hash is for linking this transaction from reveal
    def get_reference_hash(self):
        return SHA256.new(self.pack() + Serializer.write_signature(self.signature)).digest()

class RevealRandomTransaction():
    def parse(self, raw_data):
        deserializer = Deserializer(raw_data)
        self.commit_hash = deserializer.parse_hash()
        self.key = deserializer.parse_private_key()
        self.len = deserializer.get_len()
    
    def pack(self):
        raw = self.commit_hash
        raw += Serializer.write_private_key(self.key)
        return raw
    
    def get_len(self):
        return self.len

    def get_hash(self):
        return SHA256.new(self.pack()).digest()