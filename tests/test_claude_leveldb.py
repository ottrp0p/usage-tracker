"""Synthetic LevelDB files exercise framing without reading private app data."""
import base64
import json
from pathlib import Path
import shutil
import struct
import subprocess
import unittest


HELPER = Path(__file__).resolve().parents[1] / 'usage_tracker/collectors/claude_leveldb.js'


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def crc_mask(data):
    # Independent bitwise CRC implementation catches a shared checksum bug in
    # the production lookup table and generated file fixtures.
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    crc = (~crc) & 0xFFFFFFFF
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def batch(items, sequence=1):
    out = struct.pack('<QI', sequence, len(items))
    for key, value in items:
        out += bytes([0 if value is None else 1]) + varint(len(key)) + key
        if value is not None:
            out += varint(len(value)) + value
    return out


def physical(data, kind=1):
    return struct.pack('<IHB', crc_mask(bytes([kind]) + data), len(data), kind) + data


def wal(logical):
    # Split one logical record exactly where LevelDB's 32 KiB blocks end.
    out = b''
    for index, start in enumerate(range(0, len(logical), 32761)):
        part = logical[start:start + 32761]
        last = start + len(part) == len(logical)
        kind = 1 if index == 0 and last else 2 if index == 0 else 4 if last else 3
        out += physical(part, kind)
    return out


def snappy_literal(data):
    n = len(data) - 1
    if n < 60:
        tag = bytes([n << 2])
    else:
        width = (n.bit_length() + 7) // 8
        tag = bytes([(59 + width) << 2]) + n.to_bytes(width, 'little')
    return varint(len(data)) + tag + data


def block(items):
    # One restart followed by a shared-prefix entry exercises key reconstruction.
    out, prior = b'', b''
    for key, value in items:
        shared = 0
        while shared < min(len(prior), len(key)) and prior[shared] == key[shared]:
            shared += 1
        out += varint(shared) + varint(len(key) - shared) + varint(len(value))
        out += key[shared:] + value
        prior = key
    return out + struct.pack('<II', 0, 1)


def stored_block(data, compressed=False):
    payload = snappy_literal(data) if compressed else data
    payload += bytes([1 if compressed else 0])
    return payload + struct.pack('<I', crc_mask(payload))


def table(items, compressed=False):
    internal = [(key + struct.pack('<Q', ((100 - i) << 8) | (1 if value is not None else 0)), value or b'')
                for i, (key, value) in enumerate(items)]
    data = stored_block(block(internal), compressed)
    handle = varint(0) + varint(len(data) - 5)
    index = stored_block(block([(internal[-1][0], handle)]))
    footer = (varint(0) + varint(0) + varint(len(data)) + varint(len(index) - 5)).ljust(40, b'\0')
    return data + index + footer + bytes.fromhex('57fb808b247547db')


@unittest.skipUnless(shutil.which('node'), 'Node is required for the Chromium cache adapter')
class ClaudeLevelDbTests(unittest.TestCase):
    def extract(self, content, kind='.log'):
        script = """
const fs=require('node:fs'),m=require(process.argv[1]);
const x=JSON.parse(fs.readFileSync(0,'utf8'));
const r=m.extractValuesBuffer(Buffer.from(x.data,'base64'),x.kind);
process.stdout.write(JSON.stringify({values:r.values.map(b=>b.toString('base64')),warnings:r.warnings}));
"""
        run = subprocess.run(['node', '-e', script, str(HELPER)], input=json.dumps({
            'data': base64.b64encode(content).decode(), 'kind': kind,
        }), text=True, capture_output=True, check=True, timeout=10)
        result = json.loads(run.stdout)
        return [base64.b64decode(value) for value in result['values']], result['warnings']

    def test_wal_retains_historical_values_and_skips_tombstones(self):
        content = physical(batch([(b'k', b'old')])) + physical(batch([(b'k', b'new'), (b'k', None)], 2))
        self.assertEqual(self.extract(content), ([b'old', b'new'], []))

    def test_wal_reassembles_multiple_physical_blocks(self):
        value = b'a' * 80000
        self.assertEqual(self.extract(wal(batch([(b'k', value)]))), ([value], []))

    def test_truncated_tail_preserves_completed_batch(self):
        content = physical(batch([(b'a', b'good')])) + physical(batch([(b'b', b'incomplete')]))[:-2]
        values, warnings = self.extract(content)
        self.assertEqual(values, [b'good'])
        self.assertIn('leveldb_truncated_record', warnings)

    def test_checksum_failure_never_emits_corrupt_batch(self):
        bad = bytearray(physical(batch([(b'k', b'damaged')])))
        bad[-1] ^= 1
        values, warnings = self.extract(bytes(bad) + physical(batch([(b'k', b'good')])))
        self.assertEqual(values, [b'good'])
        self.assertIn('leveldb_record_checksum_mismatch', warnings)

    def test_incomplete_fragment_does_not_become_batch(self):
        values, warnings = self.extract(physical(batch([(b'k', b'v')]), 2))
        self.assertEqual(values, [])
        self.assertIn('leveldb_incomplete_fragment', warnings)

    def test_bad_atomic_batch_discards_earlier_entries(self):
        # A valid physical CRC does not make an incomplete logical batch valid.
        values, warnings = self.extract(physical(batch([(b'a', b'first'), (b'b', b'last')])[:-1]))
        self.assertEqual(values, [])
        self.assertTrue(warnings)

    def test_orphan_fragment_is_rejected(self):
        values, warnings = self.extract(physical(batch([(b'k', b'v')]), 4))
        self.assertEqual(values, [])
        self.assertIn('leveldb_orphan_fragment', warnings)

    def test_plain_sst_shared_keys_and_tombstones(self):
        self.assertEqual(self.extract(table([(b'same', b'v1'), (b'same2', b'v2'), (b'same3', None)]), '.sst'), ([b'v1', b'v2'], []))

    def test_compressed_sstable(self):
        self.assertEqual(self.extract(table([(b'k', b'a' * 300)], compressed=True), '.ldb'), ([b'a' * 300], []))

    def test_table_checksum_failure_drops_block(self):
        content = bytearray(table([(b'k', b'v')]))
        content[6] ^= 1
        values, warnings = self.extract(bytes(content), '.ldb')
        self.assertEqual(values, [])
        self.assertIn('leveldb_block_checksum_mismatch', warnings)

    def test_table_footer_truncation_is_reported(self):
        values, warnings = self.extract(table([(b'k', b'v')])[:-1], '.ldb')
        self.assertEqual(values, [])
        self.assertIn('leveldb_invalid_table_footer', warnings)

    def test_snappy_overlap_and_all_copy_forms(self):
        # Literal 'abc', then copy-length 6 at offset 3 using 1/2/4-byte forms.
        script = """
const m=require(process.argv[1]);
const samples=['09086162630903','0908616263160300','09086162631703000000'];
console.log(JSON.stringify(samples.map(h=>m.snappyRaw(Buffer.from(h,'hex')).toString())));
"""
        out = subprocess.check_output(['node', '-e', script, str(HELPER)], text=True)
        self.assertEqual(json.loads(out), ['abcabcabc'] * 3)

    def test_snappy_refuses_invalid_backreference_and_size_bomb(self):
        script = """
const m=require(process.argv[1]);const results=[];
for(const h of ['09086162631705000000','81808010']){try{m.snappyRaw(Buffer.from(h,'hex'));results.push('accepted');}catch(e){results.push(e.message);}}
console.log(JSON.stringify(results));
"""
        out = subprocess.check_output(['node', '-e', script, str(HELPER)], text=True)
        self.assertEqual(json.loads(out), ['snappy_invalid_copy', 'snappy_size_limit'])

    def test_unsupported_file_kind(self):
        self.assertEqual(self.extract(b'', '.json'), ([], ['leveldb_unsupported_file']))


if __name__ == '__main__':
    unittest.main()
