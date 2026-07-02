import hashlib, struct, time

TAG = b"/cpu-test/"

def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()

def varint(n: int) -> bytes:
    if n < 0xfd:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xffffffff:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)

def le_hex(h: str) -> bytes:
    return bytes.fromhex(h)[::-1]

def bits_to_target(bits_hex: str) -> int:
    b = bytes.fromhex(bits_hex)
    exp = b[0]
    mant = int.from_bytes(b[1:], "big")
    return mant * (1 << (8 * (exp - 3)))

def merkle_root(txids):
    layer = [le_hex(x) for x in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256d(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0]

def build_coinbase(height, value, payout_script_hex, extranonce, witness_commitment_hex=None):
    """Build coinbase transaction.

    Important consensus rule:
    Only serialize the coinbase with witness marker/flag when a valid witness
    commitment output is present. If getblocktemplate does not provide
    default_witness_commitment, returning a witness-serialized coinbase would
    make Bitcoin Core reject the block with unexpected-witness.
    """
    height_bytes = height.to_bytes((height.bit_length() + 7) // 8 or 1, "little")
    extra_bytes = struct.pack("<I", extranonce & 0xffffffff)
    script_sig = bytes([len(height_bytes)]) + height_bytes + TAG + extra_bytes
    payout_script = bytes.fromhex(payout_script_hex)
    outputs = [(value, payout_script)]
    have_witness_commitment = bool(witness_commitment_hex)
    if have_witness_commitment:
        outputs.append((0, bytes.fromhex(witness_commitment_hex)))

    base = b""
    base += struct.pack("<I", 2)
    base += varint(1)
    base += b"\x00" * 32
    base += struct.pack("<I", 0xffffffff)
    base += varint(len(script_sig)) + script_sig
    base += struct.pack("<I", 0xffffffff)
    base += varint(len(outputs))
    for amount, script in outputs:
        base += struct.pack("<Q", amount)
        base += varint(len(script)) + script
    base += struct.pack("<I", 0)

    txid = sha256d(base)[::-1].hex()

    if not have_witness_commitment:
        return base, base, txid

    witness_tx = b""
    witness_tx += struct.pack("<I", 2)
    witness_tx += b"\x00\x01"
    witness_tx += varint(1)
    witness_tx += b"\x00" * 32
    witness_tx += struct.pack("<I", 0xffffffff)
    witness_tx += varint(len(script_sig)) + script_sig
    witness_tx += struct.pack("<I", 0xffffffff)
    witness_tx += varint(len(outputs))
    for amount, script in outputs:
        witness_tx += struct.pack("<Q", amount)
        witness_tx += varint(len(script)) + script
    # Coinbase witness reserved value: 32 zero bytes.
    witness_tx += b"\x01\x20" + (b"\x00" * 32)
    witness_tx += struct.pack("<I", 0)
    return base, witness_tx, txid

def make_job_from_template(tmpl, payout_script_hex, extranonce):
    witness_commitment = tmpl.get("default_witness_commitment")
    _, coinbase_full, coinbase_txid = build_coinbase(
        tmpl["height"], tmpl["coinbasevalue"], payout_script_hex, extranonce, witness_commitment
    )
    txs = tmpl["transactions"]
    txids = [coinbase_txid] + [tx.get("txid", tx["hash"]) for tx in txs]
    root = merkle_root(txids)
    version = tmpl["version"]
    prev = le_hex(tmpl["previousblockhash"])
    bits_le = bytes.fromhex(tmpl["bits"])[::-1]
    target = bits_to_target(tmpl["bits"])
    curtime = max(int(time.time()), tmpl["curtime"])
    header_prefix = struct.pack("<I", version) + prev + root + struct.pack("<I", curtime) + bits_le
    assert len(header_prefix) == 76
    return {
        "height": tmpl["height"],
        "transactions": len(txs),
        "previousblockhash": tmpl["previousblockhash"],
        "bits": tmpl["bits"],
        "target_hex": f"{target:064x}",
        "target_int": target,
        "header_prefix_hex": header_prefix.hex(),
        "coinbase_full_hex": coinbase_full.hex(),
        "txs": txs,
        "extranonce": extranonce,
        "curtime": curtime,
    }

def build_block_hex(job, nonce: int) -> str:
    header = bytes.fromhex(job["header_prefix_hex"]) + struct.pack("<I", nonce & 0xffffffff)
    block = header + varint(1 + len(job["txs"])) + bytes.fromhex(job["coinbase_full_hex"])
    for tx in job["txs"]:
        block += bytes.fromhex(tx["data"])
    return block.hex()
