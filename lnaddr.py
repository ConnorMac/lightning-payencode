#! /usr/bin/env python3
from bech32 import bech32_encode, bech32_decode, convertbits, CHARSET

import argparse
import base58
import hashlib
import re
import secp256k1
import sys
import time


# Represent as a big-endian 32-bit number.
def u32list(val):
    assert val < (1 << 32)
    return bytearray([(val >> 24) & 0xff, (val >> 16) & 0xff, (val >> 8) & 0xff, val & 0xff])

# Encode directly as a big-endian 35-bit number (for timestamps)
def to_u35(val):
    assert val < (1 << 35)
    ret = []
    for i in range(0,7):
        ret.append(val % 32)
        val //= 32
    ret.reverse()
    return ret

# Represent big-endian number with as many 0-31 values as it takes.
def to_5bit(val):
    ret = []
    while val != 0:
        ret.append(val % 32)
        val //= 32
    ret.reverse()
    return ret

base58_prefix_map = { 'bc' : (0, 5),
                      'tb' : (111, 196) }

def is_p2pkh(currency, prefix):
    return prefix == base58_prefix_map[currency][0]

def is_p2sh(currency, prefix):
    return prefix == base58_prefix_map[currency][1]

def from_u32list(l):
    return (l[0] << 24) + (l[1] << 16) + (l[2] << 8) + l[3]

def from_u35(l):
    ret = 0
    for i in range(0,7):
        ret = ret * 32 + l[i]
    return ret

def from_5bit(l):
    total = 0
    for v in l:
        total = total * 32 + v
    return total

def tagged_unconv(char, bits):
    assert len(bits) < (1 << 10)
    return [CHARSET.find(char), len(bits) >> 5, len(bits) & 31] + bits

def tagged(char, l):
    return tagged_unconv(char, convertbits(l, 8, 5))

# Try to pull out tagged data: returns tag, tagged data and remainder.
def pull_tagged(data):
    if len(data) < 3:
        sys.exit("Truncated field")
    length = data[1] * 32 + data[2]
    if length > len(data) - 3:
        sys.exit("Truncated {} field: expected {} values"
                 .format(CHARSET[data[0]], length))
    return (CHARSET[data[0]], data[3:3+length], data[3+length:])

def lnencode(options):
    if options.no_amount:
        amount = ''
    else:
        picobtc = int(options.amount * 10**12)
        # We can only send down to millisatoshi.
        if picobtc % 10:
            sys.exit("Cannot encode {}: too many decimal places"
                     .format(options.amount))
        if picobtc % 10**12 == 0:
            amount = str(picobtc // 10**12)
        elif picobtc % 10**9 == 0:
            amount = str(picobtc // 10**9) + 'm'
        elif picobtc % 10**6 == 0:
            amount = str(picobtc // 10**6) + 'u'
        elif picobtc % 10**3 == 0:
            amount = str(picobtc // 10**3) + 'n'
        else:
            amount = str(picobtc) + 'p'

    hrp = 'ln' + options.currency + amount

    # timestamp
    now = int(time.time())
    data = to_u35(now)

    # Payment hash
    data = data + tagged('p', bytearray.fromhex(options.paymenthash))

    for r in options.route:
        pubkey,channel,fee,cltv = r.split('/')
        route = bytearray.fromhex(pubkey) + bytearray.fromhex(channel) + u32list(int(fee)) + u32list(int(cltv))
        data = data + tagged('r', route)

    if options.fallback:
        # Fallback parsing is per-currency, by definition.
        if options.currency == 'bc' or options.currency == 'tb':
            fbhrp, witness = bech32_decode(options.fallback)
            if fbhrp:
                if fbhrp != options.currency:
                    sys.exit("Not a bech32 address for this currency")
                wver = witness[0]
                if wver > 16:
                    sys.exit("Invalid witness version {}".format(witness[0]))
                wprog = witness[1:]
            else:
                addr = base58.b58decode_check(options.fallback)
                if is_p2pkh(options.currency, addr[0]):
                    wver = 17
                elif is_p2sh(options.currency, addr[0]):
                    wver = 18
                else:
                    sys.exit("Unknown address type for {}"
                             .format(options.currency))
                wprog = convertbits(addr[1:], 8, 5)
            data = data + tagged_unconv('f', [wver] + wprog)
        # Other currencies here....
        else:
            sys.exit("FIXME: Add support for parsing this currency")
    
    if options.description:
        data = data + tagged('d', [ord(c) for c in options.description])

    if options.expires:
        data = data + tagged_unconv('x', to_5bit(options.expires))
        
    if options.description_hashed:
        data = data + tagged('h', hashlib.sha256(options.description_hashed.encode('utf-8')).digest())

    # We actually sign the hrp, then the array of 5-bit values as bytes.
    privkey = secp256k1.PrivateKey(bytes(bytearray.fromhex(options.privkey)))
    sig = privkey.ecdsa_sign_recoverable(bytearray([ord(c) for c in hrp] + data))
    # This doesn't actually serialize, but returns a pair of values :(
    sig,recid = privkey.ecdsa_recoverable_serialize(sig)
    data = data + convertbits(bytes(sig) + bytes([recid]), 8, 5)

    print(bech32_encode(hrp, data))

def lndecode(options):
    hrp, data = bech32_decode(options.lnaddress)
    if not hrp:
        sys.exit("Bad bech32 checksum")

    if not hrp.startswith('ln'):
        sys.exit("Does not start with ln")

    # Final signature takes 104 bytes (65 bytes base32 encoded)
    if len(data) < 103:
        sys.exit("Too short to contain signature")
    sigdecoded = convertbits(data[-104:], 5, 8, False)
    data = data[:-104]

    pubkey = secp256k1.PublicKey(flags=secp256k1.ALL_FLAGS)
    sig = pubkey.ecdsa_recoverable_deserialize(sigdecoded[0:64], sigdecoded[64])
    pubkey.public_key = pubkey.ecdsa_recover(bytearray([ord(c) for c in hrp] + data), sig)
    print("Signed with public key: {}".format(bytearray(pubkey.serialize()).hex()))

    m = re.search("[^\d]+", hrp[2:])
    currency = m.group(0)
    print("Currency: {}".format(currency))

    amountstr = hrp[2+m.end():]
    if amountstr != '':
        # Postfix?
        if amountstr.endswith('p'):
            mul = 1
            amountstr = amountstr[:-1]
        elif amountstr.endswith('n'):
            mul = 10**3
            amountstr = amountstr[:-1]
        elif amountstr.endswith('u'):
            mul = 10**6
            amountstr = amountstr[:-1]
        elif amountstr.endswith('m'):
            mul = 10**9
            amountstr = amountstr[:-1]
        picobtc = int(amountstr) * mul
        print("Amount: {}".format(picobtc / 10**12))

        if options.rate:
            print("(Conversion: {})".format(picobtc / 10**12 * float(options.rate)))

    if len(data) < 7:
        sys.exit("Not long enough to contain timestamp")

    tstamp = from_u35(data[:7])
    data = data[7:]
    print("Timestamp: {} ({})".format(tstamp, time.ctime(tstamp)))

    while len(data) > 0:
        tag,tagdata,data = pull_tagged(data)
        if tag == 'r':
            tagdata = convertbits(tagdata, 5, 8, False)
            if len(tagdata) != 33 + 8 + 4 + 4:
                sys.exit('Unexpected r tag length {}'.format(len(tagdata)))
            print("Route: {}/{}/{}/{}"
                  .format(bytearray(tagdata[0:33]).hex(),
                          bytearray(tagdata[33:41]).hex(),
                          from_u32list(tagdata[41:45]),
                          from_u32list(tagdata[45:49])))
        elif tag == 'f':
            if currency == 'bc' or currency == 'tb':
                wver = tagdata[0]
                if wver == 17:
                    addr=base58.b58encode_check(bytes([base58_prefix_map[currency][0]]
                                                      + convertbits(tagdata[1:], 5, 8, False)))
                elif wver == 18:
                    addr=base58.b58encode_check(bytes([base58_prefix_map[currency][1]]
                                                      + convertbits(tagdata[1:], 5, 8, False)))
                elif wver <= 16:
                    addr=bech32_encode(currency, tagdata)
                else:
                    sys.exit('Invalid witness version {}'.format(wver))

            # Other currencies here...
            else:
                addr=bytearray(tagdata).hex()
            print("Fallback: {}".format(addr))
        elif tag == 'd':
            tagdata = convertbits(tagdata, 5, 8, False)
            print("Description: {}".format(''.join(chr(c) for c in tagdata)))
        elif tag == 'h':
            tagdata = convertbits(tagdata, 5, 8, False)
            print("Description hash: {}".format(bytearray(tagdata).hex()))
        elif tag == 'x':
            print("Expiry (seconds): {}".format(from_5bit(tagdata)))
        elif tag == 'p':
            tagdata = convertbits(tagdata, 5, 8, False)
            assert len(tagdata) == 32
            print("Payment hash: {}".format(bytearray(tagdata).hex()))
        else:
            tagdata = convertbits(tagdata, 5, 8, False)
            print("UNKNOWN TAG {}: {}".format(tag, bytearray(tagdata).hex()))


parser = argparse.ArgumentParser(description='Encode lightning address')
subparsers = parser.add_subparsers(dest='subparser_name',
                                   help='sub-command help')

parser_enc = subparsers.add_parser('encode', help='encode help')
parser_dec = subparsers.add_parser('decode', help='decode help')

parser_enc.add_argument('--currency', default='bc',
                    help="What currency")
parser_enc.add_argument('--route', action='append', default=[],
                        help="Extra route steps of form pubkey/channel/fee/cltv")
parser_enc.add_argument('--fallback',
                        help='Fallback address for onchain payment')
parser_enc.add_argument('--description',
                        help='What is being purchased')
parser_enc.add_argument('--description-hashed',
                        help='What is being purchased (for hashing)')
parser_enc.add_argument('--expires', type=int,
                        help='Seconds before offer expires')
parser_enc.add_argument('--no-amount', action="store_true",
                        help="Don't encode amount")
parser_enc.add_argument('amount', type=float, help='Amount in currency')
parser_enc.add_argument('paymenthash', help='Payment hash (in hex)')
parser_enc.add_argument('privkey', help='Private key (in hex)')
parser_enc.set_defaults(func=lnencode)

parser_dec.add_argument('lnaddress', help='Address to decode')
parser_dec.add_argument('--rate', type=float, help='Convfersion amount for 1 currency unit')
parser_dec.add_argument('--pubkey', help='Public key for the chanid')
parser_dec.set_defaults(func=lndecode)

options = parser.parse_args()
if not options.subparser_name:
    parser.print_help()
else:
    options.func(options)
