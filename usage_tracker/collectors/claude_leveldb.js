'use strict';

// Offline readers for Claude's Chromium IndexedDB files. Opening LevelDB itself
// would acquire locks or recover its journal. These readers only read immutable
// byte snapshots and retain historical values: provider message IDs are deduped
// by the Python store after the cache adapter removes all conversation content.
const fs = require('node:fs');
const path = require('node:path');
const MAX_FILE = 128 * 1024 * 1024;
const MAX_VALUE = 32 * 1024 * 1024;
const MAX_TOTAL = 64 * 1024 * 1024;
const MAX_VALUES = 100000;
const WAL_BLOCK = 32768;
const TABLE_MAGIC = Buffer.from('57fb808b247547db', 'hex');

// LevelDB uses Castagnoli CRC32C, then rotates/adds a mask before storing it.
// Validate every physical journal record and table block before decoding data;
// damaged bytes must never become plausible-looking usage observations.
const CRC_TABLE = Array.from({length: 256}, (_, n) => {
  let crc = n;
  for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0x82f63b78 : 0);
  return crc >>> 0;
});
function crc32c(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = CRC_TABLE[(crc ^ byte) & 255] ^ (crc >>> 8);
  return (~crc) >>> 0;
}
function maskedCrc(bytes) {
  const crc = crc32c(bytes);
  return ((((crc >>> 15) | (crc << 17)) >>> 0) + 0xa282ead8) >>> 0;
}
function readVarint(bytes, start = 0, end = bytes.length) {
  let value = 0, position = start, shift = 0;
  while (position < end && shift <= 49) {
    const byte = bytes[position++];
    value += (byte & 127) * 2 ** shift;
    if (!Number.isSafeInteger(value)) throw Error('invalid_varint');
    if (!(byte & 128)) return [value, position];
    shift += 7;
  }
  throw Error('truncated_varint');
}
function lengthPrefixed(bytes, position, end = bytes.length) {
  let length;
  [length, position] = readVarint(bytes, position, end);
  if (length > MAX_VALUE || position + length > end) throw Error('invalid_length');
  return [bytes.subarray(position, position + length), position + length];
}

// Snappy's raw block format uses overlapping backreferences. A simple byte copy
// is intentional: Buffer.copy() does not implement this repeated-copy behavior.
function snappyRaw(bytes) {
  let [length, position] = readVarint(bytes);
  if (length > MAX_VALUE) throw Error('snappy_size_limit');
  const output = Buffer.alloc(length);
  let written = 0;
  while (position < bytes.length) {
    const tag = bytes[position++];
    let count, offset;
    if ((tag & 3) === 0) {
      count = tag >>> 2;
      if (count < 60) count++;
      else {
        const extra = count - 59;
        if (position + extra > bytes.length) throw Error('snappy_truncated_literal');
        count = bytes.readUIntLE(position, extra) + 1;
        position += extra;
      }
      if (position + count > bytes.length || written + count > length) throw Error('snappy_invalid_literal');
      bytes.copy(output, written, position, position + count);
      position += count;
      written += count;
    } else {
      const extra = (tag & 3) === 1 ? 1 : (tag & 3) === 2 ? 2 : 4;
      if (position + extra > bytes.length) throw Error('snappy_truncated_copy');
      if (extra === 1) {
        count = 4 + ((tag >>> 2) & 7);
        offset = ((tag & 224) << 3) | bytes[position++];
      } else {
        count = 1 + (tag >>> 2);
        offset = bytes.readUIntLE(position, extra);
        position += extra;
      }
      if (offset < 1 || offset > written || written + count > length) throw Error('snappy_invalid_copy');
      for (let i = 0; i < count; i++) output[written + i] = output[written + i - offset];
      written += count;
    }
  }
  if (written !== length) throw Error('snappy_length_mismatch');
  return output;
}
function collector() {
  const values = [], warnings = new Set();
  let total = 0;
  return {
    values, warnings,
    add(value) {
      if (value.length > MAX_VALUE || values.length >= MAX_VALUES || total + value.length > MAX_TOTAL) {
        warnings.add('leveldb_value_limit');
        return;
      }
      values.push(value);
      total += value.length;
    },
  };
}
function parseWriteBatch(bytes) {
  if (bytes.length < 12) throw Error('leveldb_truncated_batch');
  const count = bytes.readUInt32LE(8);
  if (count > MAX_VALUES) throw Error('leveldb_batch_limit');
  let position = 12;
  const values = [];
  // Discard an entire malformed batch. A partial batch cannot establish a
  // trustworthy observation even when its first entries happen to look valid.
  for (let i = 0; i < count; i++) {
    if (position >= bytes.length) throw Error('leveldb_truncated_batch');
    const type = bytes[position++];
    let key, value;
    [key, position] = lengthPrefixed(bytes, position);
    if (type === 1) {
      [value, position] = lengthPrefixed(bytes, position);
      values.push(value);
    } else if (type !== 0) throw Error('leveldb_unknown_batch_type');
  }
  if (position !== bytes.length) throw Error('leveldb_batch_length_mismatch');
  return values;
}
function readWal(bytes, result) {
  let position = 0, fragments = [], fragmentBytes = 0;
  function clearFragments() { fragments = []; fragmentBytes = 0; }
  function complete(data) {
    try { for (const value of parseWriteBatch(data)) result.add(value); }
    catch (error) { result.warnings.add(error.message); }
  }
  while (position < bytes.length) {
    const blockEnd = Math.min((Math.floor(position / WAL_BLOCK) + 1) * WAL_BLOCK, bytes.length);
    if (blockEnd - position < 7) {
      if (bytes.subarray(position, blockEnd).some(byte => byte !== 0)) result.warnings.add('leveldb_truncated_record');
      position = blockEnd;
      continue;
    }
    const length = bytes.readUInt16LE(position + 4), type = bytes[position + 6];
    if (length === 0 && type === 0) {
      if (bytes.subarray(position, blockEnd).some(byte => byte !== 0)) result.warnings.add('leveldb_invalid_padding');
      position = blockEnd;
      continue;
    }
    if (position + 7 + length > blockEnd) {
      result.warnings.add('leveldb_truncated_record');
      clearFragments();
      position = blockEnd;
      continue;
    }
    const data = bytes.subarray(position + 7, position + 7 + length);
    const expected = bytes.readUInt32LE(position);
    position += 7 + length;
    if (maskedCrc(Buffer.concat([Buffer.from([type]), data])) !== expected) {
      result.warnings.add('leveldb_record_checksum_mismatch');
      clearFragments();
      continue;
    }
    if (type === 1) {
      if (fragments.length) result.warnings.add('leveldb_incomplete_fragment');
      clearFragments();
      complete(data);
    } else if (type === 2) {
      if (fragments.length) result.warnings.add('leveldb_incomplete_fragment');
      fragments = [data]; fragmentBytes = data.length;
    } else if (type === 3 || type === 4) {
      if (!fragments.length) { result.warnings.add('leveldb_orphan_fragment'); continue; }
      fragments.push(data); fragmentBytes += data.length;
      if (fragmentBytes > MAX_TOTAL) { result.warnings.add('leveldb_fragment_limit'); clearFragments(); continue; }
      if (type === 4) { complete(Buffer.concat(fragments)); clearFragments(); }
    } else {
      result.warnings.add('leveldb_unknown_record_type');
      clearFragments();
    }
  }
  if (fragments.length) result.warnings.add('leveldb_incomplete_fragment');
}
function blockEntries(bytes) {
  if (bytes.length < 4) throw Error('leveldb_short_block');
  const restartCount = bytes.readUInt32LE(bytes.length - 4);
  const end = bytes.length - 4 - 4 * restartCount;
  if (!restartCount || end < 0) throw Error('leveldb_invalid_restarts');
  let previousRestart = -1;
  const restartOffsets = new Set();
  for (let i = 0; i < restartCount; i++) {
    const restart = bytes.readUInt32LE(end + 4 * i);
    if (restart > end || restart <= previousRestart || (i === 0 && restart !== 0)) throw Error('leveldb_invalid_restarts');
    restartOffsets.add(restart); previousRestart = restart;
  }
  let position = 0, previousKey = Buffer.alloc(0), keyBytes = 0;
  const entries = [], entryOffsets = new Set();
  while (position < end) {
    const entryStart = position;
    let shared, suffixLength, valueLength;
    [shared, position] = readVarint(bytes, position, end);
    [suffixLength, position] = readVarint(bytes, position, end);
    [valueLength, position] = readVarint(bytes, position, end);
    if (shared > previousKey.length || shared + suffixLength > 1024 * 1024 || valueLength > MAX_VALUE ||
        position + suffixLength + valueLength > end || (restartOffsets.has(entryStart) && shared !== 0)) throw Error('leveldb_invalid_entry');
    // Prefix compression can expand a tiny block into many enormous keys.
    // Bound reconstructed key memory independently of compressed/value bytes.
    keyBytes += shared + suffixLength;
    if (keyBytes > MAX_TOTAL) throw Error('leveldb_key_size_limit');
    const key = Buffer.concat([previousKey.subarray(0, shared), bytes.subarray(position, position + suffixLength)]);
    position += suffixLength;
    const value = bytes.subarray(position, position + valueLength);
    position += valueLength;
    entries.push({key, value}); entryOffsets.add(entryStart); previousKey = key;
    if (entries.length > MAX_VALUES) throw Error('leveldb_entry_limit');
  }
  if (end > 0 && [...restartOffsets].some(offset => !entryOffsets.has(offset))) throw Error('leveldb_invalid_restarts');
  return entries;
}
function readTable(bytes, result) {
  if (bytes.length < 48 || !bytes.subarray(-8).equals(TABLE_MAGIC)) throw Error('leveldb_invalid_table_footer');
  const footer = bytes.subarray(-48);
  let ignored, position;
  [ignored, position] = readVarint(footer, 0, 40);
  [ignored, position] = readVarint(footer, position, 40);
  function readBlock(handle) {
    let offset, size, end;
    [offset, end] = readVarint(handle);
    [size] = readVarint(handle, end);
    if (size > MAX_VALUE || offset + size + 5 > bytes.length - 48) throw Error('leveldb_invalid_block_handle');
    const dataAndType = bytes.subarray(offset, offset + size + 1);
    if (maskedCrc(dataAndType) !== bytes.readUInt32LE(offset + size + 1)) throw Error('leveldb_block_checksum_mismatch');
    const compression = bytes[offset + size];
    if (compression === 0) return bytes.subarray(offset, offset + size);
    if (compression === 1) return snappyRaw(bytes.subarray(offset, offset + size));
    throw Error('leveldb_unknown_block_compression');
  }
  const indexEntries = blockEntries(readBlock(footer.subarray(position, 40)));
  for (const entry of indexEntries) {
    try {
      const entries = blockEntries(readBlock(entry.value));
      const values = [];
      for (const item of entries) {
        // The trailing eight bytes are LevelDB's sequence/type tag, not part
        // of the IndexedDB user key. Deletions carry no usage value to decode.
        if (item.key.length < 8) throw Error('leveldb_invalid_internal_key');
        const type = item.key[item.key.length - 8];
        if (type === 1) values.push(item.value);
        else if (type !== 0) throw Error('leveldb_unknown_value_type');
      }
      for (const value of values) result.add(value);
    } catch (error) { result.warnings.add(error.message); }
  }
}
function extractValuesBuffer(bytes, kind) {
  const result = collector();
  if (!Buffer.isBuffer(bytes) || bytes.length > MAX_FILE) result.warnings.add('leveldb_file_size_limit');
  else {
    try {
      if (kind === '.log') readWal(bytes, result);
      else if (kind === '.ldb' || kind === '.sst') readTable(bytes, result);
      else result.warnings.add('leveldb_unsupported_file');
    } catch (error) { result.warnings.add(error.message); }
  }
  return {values: result.values, warnings: [...result.warnings]};
}
function extractValues(filePath) {
  // Only numbered data files under the explicit Claude IndexedDB source are
  // eligible. Never inspect Chromium cookies, account databases, or other apps.
  const parent = path.basename(path.dirname(filePath));
  if (parent !== 'https_claude.ai_0.indexeddb.leveldb' || !/^\d+\.(log|ldb|sst)$/.test(path.basename(filePath))) {
    return {values: [], warnings: ['leveldb_unsupported_path']};
  }
  try {
    const fd = fs.openSync(filePath, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
    try {
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.size > MAX_FILE) return {values: [], warnings: ['leveldb_file_size_limit']};
      // Read exactly the initial size. Bytes appended during this read are left
      // for the next poll, which may also complete a truncated final record.
      const bytes = Buffer.alloc(stat.size);
      let read = 0;
      while (read < bytes.length) {
        const n = fs.readSync(fd, bytes, read, bytes.length - read, read);
        if (!n) break;
        read += n;
      }
      const result = extractValuesBuffer(bytes.subarray(0, read), path.extname(filePath));
      if (read !== stat.size) result.warnings.push('leveldb_file_changed_during_read');
      return result;
    } finally { fs.closeSync(fd); }
  } catch { return {values: [], warnings: ['leveldb_file_unreadable']}; }
}
module.exports = {extractValues, extractValuesBuffer, readVarint, snappyRaw, crc32c, maskedCrc};
