"""The reader upgrade must enrich already-checkpointed evidence in place."""

import json
from pathlib import Path
import tempfile
import unittest

from tests.test_codex import metadata, usage
from usage_tracker.config import Source
from usage_tracker.ingest import collect
from usage_tracker.storage import Store


class CodexReplayIngestTests(unittest.TestCase):
    def test_old_checkpoint_replays_and_enriches_without_duplicate_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            records = [metadata(), {"type": "turn_context", "timestamp": "2026-09-07T10:01:00Z",
                       "payload": {"model": "model-a", "turn_id": "turn-a"}}, usage(100)]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            source = Source("Codex fixture", "codex", path, "codex_desktop")
            store = Store(root / "data")
            try:
                collect(store, [source])
                row = store.conn.execute("SELECT event_key,payload FROM codex_observations").fetchone()
                payload = json.loads(row["payload"])
                payload["native"].pop("turn_context_at")
                payload["native"].pop("context_timestamp_replay_candidate")
                # Reproduce a v1 service that has consumed the entire file but
                # has not yet captured the metadata required by the new logic.
                with store.conn:
                    store.conn.execute("UPDATE codex_observations SET payload=? WHERE event_key=?",
                                       (json.dumps(payload, sort_keys=True), row["event_key"]))
                    checkpoint = store.checkpoint(str(path.resolve()))
                    checkpoint["adapter_version"] = 1
                    store.save_checkpoint(str(path.resolve()), source.name, checkpoint, {})
                collect(store, [source])
                rows = store.conn.execute("SELECT event_key,payload FROM codex_observations").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["event_key"], row["event_key"])
                native = json.loads(rows[0]["payload"])["native"]
                self.assertTrue(native["context_timestamp_replay_candidate"])
                self.assertEqual(native["turn_context_at"], "2026-09-07T10:01:00.000Z")
                # A single uncorroborated candidate remains counted. This also
                # proves upgrading provenance alone does not discard usage.
                self.assertEqual(store.conn.execute("SELECT SUM(total_tokens) FROM usage_events").fetchone()[0], 100)
                self.assertEqual(collect(store, [source])["changed_events"], 0)
            finally:
                store.close()
