from Crypto.Hash import SHA256
from chain.signed_block import SignedBlock
from serialization.serializer import Serializer, Deserializer

"""
Gossip message used for on/off chain data transfer.
Gossip can be POSITIVE or NEGATIVE
gossip- (NEGATIVE GOSSIP) is structured broadcast request send to validator about absent block in X time_slot
gossip+ (POSITIVE GOSSIP) is structured broadcast data send to requester with block tx_hash in X time_slot

gossip validation rules
- gossip can be sent by every node (simple_node, validator)
- gossip must contains:
    - sender public key
    - sender signature
    - asked block number/existing block
    - current timestamp

- negative gossip can be broadcast only once by one node per time_slot
- positive gossip must be broadcast by ALL nodes
  (3 same positive gossips by different senders means that this is correct block)
- validator node listen positive gossip validate it and add to DAG if block not exist

send negative gossip- rule
- negative gossip sends when block not received on time_slot finished

send positive gossip+ rule
- positive gossip can be sent ONLY by validator node by negative gossip- request received

"""


# negative gossip base class
class NegativeGossipTransaction:
    def __init__(self):
        # node signature
        self.signature = None
        # node public key (gossip request sender address)
        self.pubkey = None
        # current timestamp
        self.timestamp = None
        # block number
        self.number_of_block = None
        # tx length
        self.len = None

    def parse(self, raw_data):
        deserializer = Deserializer(raw_data)
        self.signature = deserializer.parse_signature()
        self.pubkey = deserializer.parse_pubkey()
        self.timestamp = deserializer.parse_timestamp()
        self.number_of_block = deserializer.parse_u32()
        self.len = deserializer.get_len()

    def pack(self):
        return Serializer.write_signature(self.signature) + \
               self.pubkey + \
               Serializer.write_timestamp(self.timestamp) + \
               Serializer.write_u32(self.number_of_block)

    def get_hash(self):
        return SHA256.new(self.pubkey +
                          Serializer.write_timestamp(self.timestamp) +
                          Serializer.write_u32(self.number_of_block)).digest()

    def get_len(self):
        return self.len

    def get_reference_hash(self):
        return SHA256.new(self.pack()).digest()


# positive gossip base class
class PositiveGossipTransaction:
    def __init__(self):
        # node signature
        self.signature = None
        # node public key (gossip request sender address)
        self.pubkey = None
        # current timestamp
        self.timestamp = None
        # returned block hash by number
        self.block_hash = None
        # tx length
        self.len = None

    def parse(self, raw_data):
        deserializer = Deserializer(raw_data)
        self.signature = deserializer.parse_signature()
        self.pubkey = deserializer.parse_pubkey()
        self.timestamp = deserializer.parse_timestamp()
        self.block_hash = deserializer.parse_hash()
        self.len = deserializer.get_len()

    def pack(self):
        return Serializer.write_signature(self.signature) + \
               self.pubkey + \
               Serializer.write_timestamp(self.timestamp) + \
               self.block_hash

    def get_hash(self):
        return SHA256.new(self.pubkey +
                          Serializer.write_timestamp(self.timestamp) +
                          self.block_hash).digest()

    def get_len(self):
        return self.len

    def get_reference_hash(self):
        return SHA256.new(self.pack()).digest()