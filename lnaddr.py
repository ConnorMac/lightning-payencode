#! /usr/bin/env python3
from bech32 import bech32_encode, bech32_decode, CHARSET
from binascii import hexlify, unhexlify
from bitstring import BitArray
from decimal import Decimal

import base58
import bitstring
import hashlib
import math
import re
import secp256k1
import sys
import time


def shorten_amount(amount):
    """ Given an amount in bitcoin, shorten it
    """
    # Convert to pico initially
    amount = int(amount * 10**12)
    units = ['p', 'n', 'u', 'm', '']
    for unit in units:
        if amount % 1000 == 0:
            amount //= 1000
        else:
            break
    return str(amount) + unit

def unshorten_amount(amount):
    """ Given a shortened amount, convert it into a decimal
    """
    units = {
        'p': 10**12,
        'n': 10**9,
        'u': 10**6,
        'm': 10**3,
    }
    unit = str(amount)[-1]
    if unit in units.keys():
        return Decimal(amount[:-1]) / units[unit]
    else:
        return Decimal(amount)

# Bech32 spits out array of 5-bit values.  Shim here.
def u5_to_bitarray(arr):
    ret = bitstring.BitArray()
    for a in arr:
        ret += bitstring.pack("uint:5", a)
    return ret

def bitarray_to_u5(barr):
    assert barr.len % 5 == 0
    ret = []
    s = bitstring.ConstBitStream(barr)
    while s.pos != s.len:
        ret.append(s.read(5).uint)
    return ret

def encode_fallback(fallback, currency):
    """ Encode all supported fallback addresses.
    """
    if currency == 'bc' or currency == 'tb':
        fbhrp, witness = bech32_decode(fallback)
        if fbhrp:
            if fbhrp != currency:
                raise ValueError("Not a bech32 address for this currency")
            wver = witness[0]
            if wver > 16:
                raise ValueError("Invalid witness version {}".format(witness[0]))
            wprog = u5_to_bitarray(witness[1:])
        else:
            addr = base58.b58decode_check(fallback)
            if is_p2pkh(currency, addr[0]):
                wver = 17
            elif is_p2sh(currency, addr[0]):
                wver = 18
            else:
                raise ValueError("Unknown address type for {}".format(currency))
            wprog = addr[1:]
        return tagged('f', bitstring.pack("uint:5", wver) + wprog)
    else:
        raise NotImplementedError("Support for currency {} not implemented".format(currency))

def parse_fallback(fallback, currency):
    if currency == 'bc' or currency == 'tb':
        wver = fallback[0:5].uint
        if wver == 17:
            addr=base58.b58encode_check(bytes([base58_prefix_map[currency][0]])
                                        + fallback[5:].tobytes())
        elif wver == 18:
            addr=base58.b58encode_check(bytes([base58_prefix_map[currency][1]])
                                        + fallback[5:].tobytes())
        elif wver <= 16:
            addr=bech32_encode(currency, bitarray_to_u5(fallback))
        else:
            raise ValueError('Invalid witness version {}'.format(wver))
    else:
        addr=fallback.tobytes()
    return addr


# Map of classical and witness address prefixes
base58_prefix_map = {
    'bc' : (0, 5),
    'tb' : (111, 196)
}

def is_p2pkh(currency, prefix):
    return prefix == base58_prefix_map[currency][0]

def is_p2sh(currency, prefix):
    return prefix == base58_prefix_map[currency][1]

# Tagged field containing BitArray
def tagged(char, l):
    # Tagged fields need to be zero-padded to 5 bits.
    while l.len % 5 != 0:
        l.append('0b0')
    return bitstring.pack("uint:5, uint:5, uint:5",
                          CHARSET.find(char),
                          (l.len / 5) / 32, (l.len / 5) % 32) + l

# Tagged field containing bytes
def tagged_bytes(char, l):
    return tagged(char, bitstring.BitArray(l))

# Discard trailing bits, convert to bytes.
def trim_to_bytes(barr):
    # Adds a byte if necessary.
    b = barr.tobytes()
    if barr.len % 8 != 0:
        return b[:-1]
    return b

# Try to pull out tagged data: returns tag, tagged data and remainder.
def pull_tagged(stream):
    tag = stream.read(5).uint
    length = stream.read(5).uint * 32 + stream.read(5).uint
    return (CHARSET[tag], stream.read(length * 5), stream)

def lnencode(addr, privkey):
    if addr.amount:
        amount = Decimal(str(addr.amount))
        # We can only send down to millisatoshi.
        if amount * 10**12 % 10:
            raise ValueError("Cannot encode {}: too many decimal places".format(
                addr.amount))

        amount = addr.currency + shorten_amount(unshorten_amount(amount))
    else:
        amount = addr.currency if addr.currency else ''

    hrp = 'ln' + amount

    # Start with the timestamp
    data = bitstring.pack('uint:35', addr.date)

    # Payment hash
    data += tagged_bytes('p', addr.paymenthash)

    for k, v in addr.tags:
        if k == 'r':
            pubkey, channel, fee, cltv = v
            route = bitstring.BitArray(pubkey) + bitstring.BitArray(channel) + bitstring.pack('intbe:64', fee) + bitstring.pack('intbe:16', cltv)
            data += tagged('r', route)
        elif k == 'f':
            data += encode_fallback(v, addr.currency)
        elif k == 'd':
            data += tagged_bytes('d', v.encode())
        elif k == 'x':
            # Get minimal length by trimming leading 5 bits at a time.
            expirybits = bitstring.pack('intbe:64', v)[4:64]
            while expirybits.startswith('0b00000'):
                expirybits = expirybits[5:]
            data += tagged('x', expirybits)
        elif k == 'h':
            data += tagged_bytes('h', hashlib.sha256(v.encode('utf-8')).digest())

    # We actually sign the hrp, then the array of 5-bit values as bytes.
    privkey = secp256k1.PrivateKey(bytes(unhexlify(privkey)))
    sig = privkey.ecdsa_sign_recoverable(bytearray([ord(c) for c in hrp] + bitarray_to_u5(data)))
    # This doesn't actually serialize, but returns a pair of values :(
    sig, recid = privkey.ecdsa_recoverable_serialize(sig)
    data += bytes(sig) + bytes([recid])

    return bech32_encode(hrp, bitarray_to_u5(data))

class LnAddr(object):
    def __init__(self, paymenthash=None, amount=None, currency='bc', tags=None, date=None):
        self.date = int(time.time()) if not date else int(date)
        self.tags = [] if not tags else tags
        self.paymenthash=paymenthash
        self.signature = None
        self.pubkey = None
        self.currency = currency
        self.amount = amount

    def __str__(self):
        return "LnAddr[{}, amount={}{} tags=[{}]]".format(
            hexlify(self.pubkey.serialize()).decode('utf-8'),
            self.amount, self.currency,
            ", ".join([k + '=' + str(v) for k, v in self.tags])
        )

def lndecode(a):
    hrp, data = bech32_decode(a)
    if not hrp:
        raise ValueError("Bad bech32 checksum")

    if not hrp.startswith('ln'):
        raise ValueError("Does not start with ln")

    data = u5_to_bitarray(data);

    # Final signature 65 bytes, split it off.
    if len(data) < 65*8:
        raise ValueError("Too short to contain signature")
    sigdecoded = data[-65*8:].tobytes()
    data = bitstring.ConstBitStream(data[:-65*8])

    addr = LnAddr()
    addr.pubkey = secp256k1.PublicKey(flags=secp256k1.ALL_FLAGS)
    addr.signature = addr.pubkey.ecdsa_recoverable_deserialize(
        sigdecoded[0:64], sigdecoded[64])
    addr.pubkey.public_key = addr.pubkey.ecdsa_recover(
        bytearray([ord(c) for c in hrp] + bitarray_to_u5(data)), addr.signature)

    m = re.search("[^\d]+", hrp[2:])
    if m:
        addr.currency = m.group(0)
        amountstr = hrp[2+m.end():]
        if amountstr != '':
            addr.amount = unshorten_amount(amountstr)

    addr.date = data.read(35).uint

    while data.pos != data.len:
        tag, tagdata, data = pull_tagged(data)
        if tag == 'r':
            tagbytes = trim_to_bytes(tagdata)
            # FIXME: Ignore if incorrect length!
            if len(tagbytes) != 33 + 8 + 8 + 2:
                raise ValueError('Unexpected r tag length {}'.format(len(tagbytes)))

            addr.tags.append(('r',(
                tagbytes[0:33],
                tagbytes[33:41],
                tagdata[41*8:49*8].intbe,
                tagdata[49*8:51*8].intbe
            )))
        elif tag == 'f':
            addr.tags.append(('f', parse_fallback(tagdata, addr.currency)))

        elif tag == 'd':
            addr.tags.append(('d', trim_to_bytes(tagdata).decode('utf-8')))

        elif tag == 'h':
            # FIXME: Ignore if incorrect length!
            addr.tags.append(('h', trim_to_bytes(tagdata)))

        elif tag == 'x':
            addr.tags.append(('x', tagdata.uint))

        elif tag == 'p':
            # FIXME: Ignore if incorrect length!
            assert len(trim_to_bytes(tagdata)) == 32
            addr.paymenthash = trim_to_bytes(tagdata)

        else:
            addr.tags[tag] = tagdata
    return addr
