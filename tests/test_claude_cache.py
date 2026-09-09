"""Synthetic Chromium cache fixtures; no real conversations or private data."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from usage_tracker.collectors.claude import parse_line
from usage_tracker.collectors.claude_cache import (
    CacheReadError, PARTIAL_HISTORY_WARNING, PROVISIONAL_WARNING,
    find_node, parse_cache, parse_metadata,
)
from usage_tracker.storage import Store


def assistant(output=3, *, final=False, message_id="msg_cache_1"):
    return {
        "type": "assistant", "session_id": "session-cache", "request_id": "req_cache_1",
        "timestamp": "2026-09-07T22:14:08.844Z", "uuid": "row-cache-1",
        "message": {
            "id": message_id, "role": "assistant", "model": "claude-fable-test",
            "stop_reason": "end_turn" if final else None,
            "content": [{"type": "text", "text": "PRIVATE_CONTENT_MUST_NOT_LEAK"}],
            "usage": {"input_tokens": 2, "output_tokens": output,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10,
                      "private": "PRIVATE_USAGE_MUST_NOT_LEAK"},
        },
    }


def decoded(*records, complete=False):
    return {"format": "claude_cache_metadata_v1", "recognized": True,
            "history_complete": complete, "records": list(records)}


def cache_object(*records, complete=False):
    return {"product": "cowork", "conversationUuid": "cowork:synthetic",
            "fetchedAt": 1788828627173, "title": "PRIVATE_TITLE_MUST_NOT_LEAK",
            "tree": {"kind": "cowork_remote", "hasOlder": not complete,
                     "events": [{"kind": "message", "seq": i, "payload": record}
                                for i, record in enumerate(records)]}}


class CacheMetadataTests(unittest.TestCase):
    def test_partial_and_provisional_are_separate_evidence(self):
        result = parse_metadata(decoded(assistant()), source_path="/cache/blob")
        event = result.events[0]
        self.assertEqual(event.total_tokens, 115)
        self.assertEqual(event.surface, "claude_desktop_agent")
        self.assertEqual(event.native["source_kind"], "desktop_conversation_cache")
        self.assertFalse(event.native["cache_history_complete"])
        self.assertFalse(event.native["usage_is_final"])
        self.assertIn("cache_partial_history", event.flags)
        self.assertIn("provisional_usage", event.flags)
        self.assertIn(PARTIAL_HISTORY_WARNING, result.warnings)
        self.assertIn(PROVISIONAL_WARNING, result.warnings)

    def test_complete_cache_does_not_imply_final_usage(self):
        result = parse_metadata(decoded(assistant(), complete=True), source_path="/cache/blob")
        self.assertNotIn("cache_partial_history", result.events[0].flags)
        self.assertIn("provisional_usage", result.events[0].flags)
        self.assertNotIn(PARTIAL_HISTORY_WARNING, result.warnings)

    def test_final_message_does_not_imply_complete_history(self):
        result = parse_metadata(decoded(assistant(final=True)), source_path="/cache/blob")
        self.assertIn("cache_partial_history", result.events[0].flags)
        self.assertNotIn("provisional_usage", result.events[0].flags)

    def test_missing_counts_stay_unknown(self):
        record = assistant()
        record["message"]["usage"] = {"input_tokens": 2}
        event = parse_metadata(decoded(record), source_path="/cache/blob").events[0]
        self.assertIsNone(event.input_tokens)
        self.assertIsNone(event.cached_input_tokens)
        self.assertIsNone(event.output_tokens)
        self.assertIsNone(event.total_tokens)

    def test_repeated_snapshots_and_later_local_final_deduplicate(self):
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)) as store:
            first = parse_metadata(decoded(assistant()), source_path="/cache/first")
            updated = parse_metadata(decoded(assistant(8)), source_path="/cache/second")
            store.ingest(first, "/cache/first")
            store.ingest(first, "/cache/first")
            store.ingest(updated, "/cache/second")
            final = parse_line(assistant(6, final=True), {}, source_path="/logs/full.jsonl", surface="claude_desktop_agent")
            self.assertEqual(first.events[0].event_key, final.events[0].event_key)
            store.ingest(final, "/logs/full.jsonl")
            store.ingest(updated, "/cache/second")
            rows = store.conn.execute("SELECT output_tokens,total_tokens,native FROM usage_events").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0][0], rows[0][1]), (6, 118))
            self.assertTrue(json.loads(rows[0][2])["usage_is_final"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM event_sources").fetchone()[0], 3)

    def test_unknown_metadata_is_retryable_but_known_nonusage_object_is_empty(self):
        with self.assertRaises(CacheReadError):
            parse_metadata({}, source_path="/cache/blob")
        self.assertFalse(parse_metadata({"format": "claude_cache_metadata_v1", "recognized": False}, source_path="/cache/blob").events)

    def test_missing_runtime_is_visible(self):
        with patch("usage_tracker.collectors.claude_cache.find_node", return_value=None):
            with self.assertRaisesRegex(CacheReadError, "requires Node.js"):
                parse_cache(Path("/cache/blob"))

    def test_timeout_and_decoder_errors_never_include_private_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob"
            path.write_bytes(b"test")
            with patch("usage_tracker.collectors.claude_cache.subprocess.run", side_effect=subprocess.TimeoutExpired("PRIVATE", 15)):
                with self.assertRaisesRegex(CacheReadError, "timed out"):
                    parse_cache(path, node_path="/synthetic/node")
            failure = subprocess.CompletedProcess([], 1, b"", b"PRIVATE_CONTENT_MUST_NOT_LEAK")
            with patch("usage_tracker.collectors.claude_cache.subprocess.run", return_value=failure):
                with self.assertRaises(CacheReadError) as error:
                    parse_cache(path, node_path="/synthetic/node")
                self.assertNotIn("PRIVATE", str(error.exception))


@unittest.skipUnless(find_node(), "Optional Node.js cache decoder is not installed")
class CacheDecoderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "blob"
        self.node = find_node()

    def write_cache(self, value, *, compressed=False):
        # Execute only this constant fixture generator, with the synthetic object
        # passed as JSON data over stdin. It cannot execute the payload's text.
        script = "const fs=require('node:fs'),v8=require('node:v8'); const o=JSON.parse(fs.readFileSync(0,'utf8')); process.stdout.write(Buffer.concat([Buffer.from([255,21,254]),Buffer.alloc(12),v8.serialize(o)]));"
        binary = subprocess.run([self.node, "-e", script], input=json.dumps(value).encode(), capture_output=True, check=True).stdout
        if compressed:
            # A valid literal-only raw Snappy block tests the Chromium wrapper
            # without depending on a compressor package or real cached data.
            length, varint = len(binary), bytearray()
            while length >= 128:
                varint.append((length & 127) | 128)
                length >>= 7
            varint.append(length)
            literal_length = len(binary) - 1
            extra = max(1, (literal_length.bit_length() + 7) // 8)
            tag = bytes([(59 + extra) << 2]) + literal_length.to_bytes(extra, "little")
            binary = b"\xff\x11\x02" + bytes(varint) + tag + binary
        self.path.write_bytes(binary)

    def test_uncompressed_and_compressed_blobs_have_same_event_identity(self):
        self.write_cache(cache_object(assistant()))
        plain = parse_cache(self.path).events[0]
        self.write_cache(cache_object(assistant()), compressed=True)
        compressed = parse_cache(self.path).events[0]
        self.assertEqual(asdict(plain), asdict(compressed))

    def test_helper_retains_summary_metadata_without_adding_it_to_events(self):
        result_summary = {"type": "result", "uuid": "result-private-test", "session_id": "session-cache", "usage": {"input_tokens": 999999, "output_tokens": 999999}}
        fake_nested = {"type": "user", "message": {"content": [assistant(message_id="msg_fake")]}}
        self.write_cache(cache_object(assistant(), result_summary, fake_nested))
        helper = Path(__file__).parents[1] / "usage_tracker/collectors/claude_cache_decode.js"
        raw = subprocess.run([self.node, str(helper), str(self.path)], capture_output=True, check=True).stdout
        self.assertNotIn(b"PRIVATE", raw)
        self.assertIn(b"999999", raw)  # Separate summary evidence is now retained.
        self.assertNotIn(b"msg_fake", raw)
        parsed = parse_cache(self.path)
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(len(parsed.summaries), 1)
        self.assertNotIn("PRIVATE", json.dumps([asdict(event) for event in parsed.events]))

    def test_native_timestamp_or_server_timestamp_never_file_or_fetch_time(self):
        native = assistant()
        fallback = assistant(message_id="msg_fallback")
        del fallback["timestamp"]
        value = cache_object(native, fallback)
        value["tree"]["events"][1]["serverCreatedAt"] = 1788819249055
        self.write_cache(value)
        records = parse_cache(self.path).events
        self.assertEqual(records[0].timestamp, "2026-09-07T22:14:08.844Z")
        self.assertEqual(records[1].timestamp, "2026-09-07T22:14:09.055Z")

    def test_invalid_counts_and_synthetic_messages_are_not_fabricated(self):
        invalid = assistant()
        invalid["message"]["usage"].update(input_tokens=True, output_tokens="123", cache_read_input_tokens=-1)
        synthetic = deepcopy(assistant(message_id="msg_synthetic"))
        synthetic["message"]["model"] = "<synthetic>"
        self.write_cache(cache_object(invalid, synthetic))
        result = parse_cache(self.path)
        self.assertEqual(len(result.events), 1)
        self.assertIsNone(result.events[0].total_tokens)

    def test_bad_header_and_truncated_compression_fail_closed(self):
        for binary, message in [(b"not a cache", "unsupported Chromium"), (b"\xff\x11\x02\x01\x02\x00\x00", "could not be decoded")]:
            with self.subTest(binary=binary):
                self.path.write_bytes(binary)
                with self.assertRaisesRegex(CacheReadError, message):
                    parse_cache(self.path)

    def test_other_indexeddb_objects_are_ignored_and_unknown_trees_reported(self):
        self.write_cache({"buster": "synthetic", "clientState": {"secret": "PRIVATE"}})
        self.assertFalse(parse_cache(self.path).events)
        self.write_cache({"product": "cowork", "tree": {"kind": "future_version", "events": []}})
        with self.assertRaisesRegex(CacheReadError, "unsupported conversation"):
            parse_cache(self.path)

    def test_leveldb_snapshot_versions_preserve_history_and_provider_identity(self):
        from tests.test_claude_leveldb import batch, physical, varint
        self.write_cache(cache_object(assistant(), complete=True))
        first = self.path.read_bytes()
        self.write_cache(cache_object(assistant(8), assistant(message_id="msg_later")))
        second = self.path.read_bytes()
        root = Path(self.directory.name) / "https_claude.ai_0.indexeddb.leveldb"
        root.mkdir()
        path = root / "000004.log"
        path.write_bytes(physical(batch([(b"same-key", varint(1) + first)])) +
                         physical(batch([(b"same-key", varint(2) + second)], 2)))
        parsed = parse_cache(path)
        self.assertEqual(len(parsed.events), 3)
        self.assertEqual(len({event.event_key for event in parsed.events}), 2)
        self.assertNotIn("cache_partial_history", parsed.events[0].flags)
        self.assertIn("cache_partial_history", parsed.events[1].flags)
        self.assertEqual(parsed.events[1].output_tokens, 8)

    def test_leveldb_table_prefix_and_ordinary_metadata_are_handled(self):
        from tests.test_claude_leveldb import table, varint
        self.write_cache(cache_object(assistant()))
        root = Path(self.directory.name) / "https_claude.ai_0.indexeddb.leveldb"
        root.mkdir()
        path = root / "000005.ldb"
        # An external-blob pointer carries no inline serialization; the ordinary
        # blob collector handles the referenced file independently.
        path.write_bytes(table([(b"conversation", varint(139) + self.path.read_bytes()),
                                (b"pointer", varint(1) + b"\xff\x11\x01\x05"),
                                (b"metadata", b"ordinary metadata")], compressed=True))
        result = parse_cache(path)
        self.assertEqual(len(result.events), 1)
        self.assertFalse(any("LevelDB" in warning for warning in result.warnings))

    def test_leveldb_bad_record_keeps_other_usage_and_reports_partial_read(self):
        from tests.test_claude_leveldb import batch, physical, varint
        self.write_cache(cache_object(assistant()))
        root = Path(self.directory.name) / "https_claude.ai_0.indexeddb.leveldb"
        root.mkdir()
        path = root / "000004.log"
        good = physical(batch([(b"conversation", varint(1) + self.path.read_bytes())]))
        bad = bytearray(physical(batch([(b"bad", b"PRIVATE_CORRUPT_DATA")])))
        bad[-1] ^= 1
        path.write_bytes(good + bytes(bad))
        result = parse_cache(path)
        self.assertEqual(len(result.events), 1)
        self.assertTrue(any("LevelDB contains unreadable" in warning for warning in result.warnings))
        self.assertNotIn("PRIVATE", json.dumps(result.warnings))


if __name__ == "__main__":
    unittest.main()
