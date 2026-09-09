"use strict";

// This local helper uses Node's built-in V8 deserializer, never eval, application
// code, credentials, or network access. Its ONLY output is bounded, allowlisted
// usage metadata. Unknown payload fields and all conversation bodies stay here.
const fs = require("node:fs");
const path = require("node:path");
const v8 = require("node:v8");
const MAX_BYTES = 32 * 1024 * 1024;
const MAX_EVENTS = 20000;

function fail(code) { throw new Error(code); }
function mapping(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function label(value) { return typeof value === "string" && value.length > 0 && value.length <= 256 ? value : undefined; }
function token(value) { return Number.isSafeInteger(value) && value >= 0 ? value : undefined; }

// Chromium's ff 11 02 wrapper contains a raw Snappy block. Bound allocation,
// every literal, and every back-reference; copies may overlap as Snappy allows.
function snappyRaw(bytes) {
  let p = 0, length = 0, shift = 0;
  while (true) {
    if (p >= bytes.length || shift > 28) fail("invalid_cache");
    const value = bytes[p++];
    length += (value & 127) * 2 ** shift;
    if (!(value & 128)) break;
    shift += 7;
  }
  if (length > MAX_BYTES) fail("size_limit");
  const output = Buffer.alloc(length);
  let written = 0;
  while (p < bytes.length) {
    const tag = bytes[p++];
    let count, offset;
    if ((tag & 3) === 0) {
      count = tag >>> 2;
      if (count < 60) count++;
      else {
        const extra = count - 59;
        count = 0;
        if (p + extra > bytes.length) fail("invalid_cache");
        for (let i = 0; i < extra; i++) count += bytes[p++] * 2 ** (8 * i);
        count++;
      }
      if (p + count > bytes.length || written + count > length) fail("invalid_cache");
      bytes.copy(output, written, p, p + count);
      p += count;
      written += count;
    } else {
      const extra = (tag & 3) === 1 ? 1 : (tag & 3) === 2 ? 2 : 4;
      if (p + extra > bytes.length) fail("invalid_cache");
      if (extra === 1) {
        count = 4 + ((tag >>> 2) & 7);
        offset = ((tag & 224) << 3) | bytes[p++];
      } else {
        count = 1 + (tag >>> 2);
        offset = bytes.readUIntLE(p, extra);
        p += extra;
      }
      if (offset < 1 || offset > written || written + count > length) fail("invalid_cache");
      for (let i = 0; i < count; i++) output[written + i] = output[written + i - offset];
      written += count;
    }
  }
  if (written !== length) fail("invalid_cache");
  return output;
}

function decode(bytes) {
  if (bytes.subarray(0, 3).equals(Buffer.from([255, 17, 2]))) bytes = snappyRaw(bytes.subarray(3));
  // This is the observed Chromium version 21 envelope with no trailer, followed
  // by V8 serialization version 15. A new format must fail visibly, never guess
  // offsets by searching private data for something that looks like a header.
  if (bytes.length < 17 || !bytes.subarray(0, 3).equals(Buffer.from([255, 21, 254])) ||
      !bytes.subarray(3, 15).equals(Buffer.alloc(12)) || bytes[15] !== 255 || bytes[16] !== 15) {
    fail("unsupported_header");
  }
  return v8.deserialize(bytes.subarray(15));
}

function usage(source) {
  const result = {};
  if (!mapping(source)) return result;
  for (const key of ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]) {
    const value = token(source[key]);
    if (value !== undefined) result[key] = value;
  }
  // Preserve only numeric cache TTL and reasoning subsets. Result iterations,
  // server tool activity, provider extensions, and arbitrary fields are omitted.
  for (const [key, fields] of [
    ["cache_creation", ["ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"]],
    ["output_tokens_details", ["thinking_tokens"]],
  ]) {
    if (!mapping(source[key])) continue;
    const detail = {};
    for (const field of fields) {
      const value = token(source[key][field]);
      if (value !== undefined) detail[field] = value;
    }
    result[key] = detail;
  }
  return result;
}

function quota(source) {
  const result = {};
  if (!mapping(source)) return result;
  for (const key of ["status", "overageStatus"]) {
    if (["allowed", "allowed_warning", "rejected"].includes(source[key])) result[key] = source[key];
  }
  const windowName = value => typeof value === "string" && /^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(value);
  if (windowName(source.rateLimitType)) result.rateLimitType = source.rateLimitType;
  if (typeof source.isUsingOverage === "boolean") result.isUsingOverage = source.isUsingOverage;
  function reset(value) {
    if (typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 8.64e12) return value;
    if (label(value) && Number.isFinite(Date.parse(value))) return value;
    return undefined;
  }
  if (reset(source.resetsAt) !== undefined) result.resetsAt = reset(source.resetsAt);
  if (mapping(source.unifiedWindows)) {
    result.unifiedWindows = Object.create(null);
    for (const [name, window] of Object.entries(source.unifiedWindows).slice(0, 64)) {
      if (!windowName(name) || !mapping(window)) continue;
      const clean = {};
      if (typeof window.utilization === "number" && Number.isFinite(window.utilization) && window.utilization >= 0 && window.utilization <= 1e9) clean.utilization = window.utilization;
      if (reset(window.resetsAt) !== undefined) clean.resetsAt = reset(window.resetsAt);
      result.unifiedWindows[name] = clean;
    }
  }
  return result;
}

function metadata(object) {
  const result = {format: "claude_cache_metadata_v1", recognized: false};
  if (!mapping(object) || !mapping(object.tree)) return result;
  // Only the verified remote Cowork cache is supported. Other cache records
  // without a conversation tree are ordinary non-usage data and are ignored.
  if (object.product !== "cowork" || object.tree.kind !== "cowork_remote" || !Array.isArray(object.tree.events)) {
    fail("unsupported_tree");
  }
  if (object.tree.events.length > MAX_EVENTS) fail("size_limit");
  result.recognized = true;
  result.history_complete = object.tree.hasOlder === false;
  result.records = [];
  for (const event of object.tree.events) {
    // Keep result/quota evidence separate in the Python parser. User records
    // supply only explicit UUID-to-time links, never their message bodies.
    if (!mapping(event) || event.kind !== "message" || !mapping(event.payload)) continue;
    const payload = event.payload;
    if (!["assistant", "user", "result", "rate_limit_event"].includes(payload.type)) continue;
    const record = {type: payload.type, entrypoint: "local-agent"};
    for (const key of ["session_id", "sessionId", "timestamp", "created_at", "request_id", "requestId", "uuid", "parent_tool_use_id", "agentId"]) {
      const value = label(payload[key]);
      if (value !== undefined) record[key] = value;
    }
    // ServerCreatedAt is an event timestamp in milliseconds; snapshot fetchedAt
    // and filesystem mtime are never request timestamps. Prefer the native one.
    if (!record.timestamp && !record.created_at && Number.isSafeInteger(event.serverCreatedAt)) {
      const date = new Date(event.serverCreatedAt);
      if (Number.isFinite(date.valueOf())) record.timestamp = date.toISOString();
    }
    if (payload.isSidechain === true) record.isSidechain = true;
    if (payload.isApiErrorMessage === true) record.isApiErrorMessage = true;
    if (payload.type === "user") {
      result.records.push(record);
      continue;
    }
    if (payload.type === "rate_limit_event") {
      record.rate_limit_info = quota(payload.rate_limit_info);
      result.records.push(record);
      continue;
    }
    if (payload.type === "result") {
      record.usage = usage(payload.usage);
      record.modelUsage = Object.create(null);
      for (const [model, counts] of Object.entries(mapping(payload.modelUsage) ? payload.modelUsage : {}).slice(0, 128)) {
        if (!label(model) || !mapping(counts)) continue;
        const clean = {};
        for (const key of ["inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"]) {
          if (token(counts[key]) !== undefined) clean[key] = token(counts[key]);
        }
        record.modelUsage[model] = clean;
      }
      for (const key of ["num_turns", "duration_ms", "duration_api_ms"]) {
        if (token(payload[key]) !== undefined) record[key] = token(payload[key]);
      }
      for (const key of ["time_origin_ms", "request_sent_wall_ms"]) {
        if (typeof payload[key] === "number" && Number.isFinite(payload[key]) && payload[key] >= 0 && payload[key] <= 8.64e15) record[key] = payload[key];
      }
      if (typeof payload.is_error === "boolean") record.is_error = payload.is_error;
      for (const [key, allowed] of [
        ["subtype", ["success", "error_during_execution", "error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"]],
        ["terminal_reason", ["completed", "interrupted", "aborted", "error", "max_turns", "max_budget_usd"]],
        ["stop_reason", ["end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded"]],
      ]) if (allowed.includes(payload[key])) record[key] = payload[key];
      if (label(payload.user_message_uuid)) record.user_message_uuid = payload.user_message_uuid;
      if (Array.isArray(payload.user_message_uuids)) record.user_message_uuids = payload.user_message_uuids.slice(0, 256).filter(value => label(value));
      result.records.push(record);
      continue;
    }
    if (!mapping(payload.message)) continue;
    const message = payload.message;
    if (message.role !== undefined && message.role !== "assistant") continue;
    record.message = {role: "assistant"};
    for (const key of ["id", "model", "request_id"]) {
      const value = label(message[key]);
      if (value !== undefined) record.message[key] = value;
    }
    if (["end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal", "model_context_window_exceeded"].includes(message.stop_reason)) {
      record.message.stop_reason = message.stop_reason;
    }
    record.message.usage = usage(message.usage);
    result.records.push(record);
  }
  return result;
}

function metadataForValues(values, sourceWarnings = []) {
  const result = {format: "claude_cache_metadata_v1", snapshots: [], warnings: []};
  let unreadable = sourceWarnings.length > 0;
  const {readVarint} = require("./claude_leveldb.js");
  for (const value of values) {
    // IndexedDB object-store values begin with a record-version varint. Only
    // inspect the known serialized value immediately after it; metadata keys,
    // ff1101 external-blob pointers, and arbitrary byte strings are not scanned.
    let offset;
    try { [, offset] = readVarint(value); } catch { continue; }
    const bytes = value.subarray(offset);
    const chromium = bytes[0] === 255 && ((bytes[1] === 21 && bytes[2] === 254) || (bytes[1] === 17 && bytes[2] === 2));
    const bareV8 = bytes[0] === 255 && bytes[1] === 15;
    if (!chromium && !bareV8) continue;
    try {
      const snapshot = metadata(bareV8 ? v8.deserialize(bytes) : decode(bytes));
      if (snapshot.recognized) result.snapshots.push(snapshot);
    } catch {
      // Keep other checksum-validated snapshots even when one serialized value
      // uses an unsupported host-object/trailer or a newer conversation tree.
      unreadable = true;
    }
  }
  if (unreadable) result.warnings.push("Claude desktop LevelDB contains unreadable records; some cached usage may be unavailable.");
  return result;
}

// Also export the pure decoder for the companion read-only LevelDB adapter and
// synthetic tests. Requiring this module must never open a file or emit data.
module.exports = {decode, metadata, metadataForValues, snappyRaw};

if (require.main === module) try {
  // Read a bounded regular file, including a second size check after reading in
  // case it grew. The caller separately checks its content hash before/after.
  const file = process.argv[2];
  const stat = fs.statSync(file);
  if (!stat.isFile()) fail("invalid_cache");
  if (stat.size > MAX_BYTES) fail("size_limit");
  let result;
  if ([".log", ".ldb", ".sst"].includes(path.extname(file))) {
    const extracted = require("./claude_leveldb.js").extractValues(file);
    result = metadataForValues(extracted.values, extracted.warnings);
  } else {
    const bytes = fs.readFileSync(file);
    if (bytes.length > MAX_BYTES) fail("size_limit");
    result = metadata(decode(bytes));
  }
  const serialized = JSON.stringify(result);
  if (Buffer.byteLength(serialized) > 16 * 1024 * 1024) fail("size_limit");
  process.stdout.write(serialized);
} catch (error) {
  const allowed = new Set(["unsupported_header", "unsupported_tree", "size_limit"]);
  process.stderr.write(allowed.has(error.message) ? error.message : "invalid_cache");
  process.exitCode = 1;
}
