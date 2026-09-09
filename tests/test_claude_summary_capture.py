"""Metadata-only result/quota evidence and explicitly linked turn windows."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from usage_tracker.collectors.claude import parse_line
from usage_tracker.collectors.claude_cache import find_node, parse_cache, parse_metadata
from usage_tracker.models import stable_key


def result_record(**extra):
    return {
        "type": "result", "uuid": "result-1", "session_id": "sdk-session",
        "created_at": "2026-09-07T22:17:07.815123Z", "subtype": "success",
        "is_error": False, "terminal_reason": "completed", "stop_reason": "end_turn",
        "time_origin_ms": 1788817932273.3235, "duration_ms": 90000, "num_turns": 5,
        "usage": {"input_tokens": 10, "output_tokens": 50,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20,
                  "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 20}},
        "modelUsage": {"claude-test": {"inputTokens": 20, "outputTokens": 300,
                                       "cacheReadInputTokens": 500, "cacheCreationInputTokens": 100}},
        "result": "PRIVATE_RESULT_TEXT", "errors": ["PRIVATE_ERROR_TEXT"], **extra,
    }


def user_record(uuid="user-1", **extra):
    return {"type": "user", "uuid": uuid, "session_id": "remote-container-session",
            "created_at": "2026-09-07T22:15:00Z", "message": {"content": "PRIVATE_USER_TEXT"}, **extra}


def quota_record():
    return {"type": "rate_limit_event", "uuid": "quota-1", "session_id": "sdk-session",
            "created_at": "2026-09-07T22:14:17.933565Z",
            "rate_limit_info": {"status": "allowed_warning", "rateLimitType": "seven_day_fable",
                "isUsingOverage": False, "unifiedWindows": {
                    "five_hour": {"utilization": .22, "resetsAt": 1788835800},
                    "seven_day_fable": {"utilization": .76},
                    "future_pool": {"utilization": True, "private": "PRIVATE_QUOTA_TEXT"}},
                "private": "PRIVATE_QUOTA_TEXT"}}


class SummaryCaptureTests(unittest.TestCase):
    def parse(self, record, state=None):
        return parse_line(record, {} if state is None else state, source_path="/audit.jsonl", surface="claude_desktop_agent")

    def test_completed_result_is_separate_evidence_with_two_scopes(self):
        parsed = self.parse(result_record())
        self.assertFalse(parsed.events)
        self.assertFalse(parsed.snapshots)
        summary = parsed.summaries[0]
        self.assertTrue(summary.terminal)
        self.assertEqual(summary.timestamp, "2026-09-07T22:17:07.815Z")
        self.assertEqual(summary.usage["input_tokens"], 10)
        self.assertEqual(summary.usage["output_tokens"], 50)
        self.assertEqual(summary.model_usage["claude-test"]["output_tokens"], 300)
        self.assertEqual(summary.native["model_usage_scope"], "sdk_call_including_subagents")

    def test_time_origin_and_duration_do_not_fabricate_run_boundary(self):
        summary = self.parse(result_record()).summaries[0]
        self.assertIsNone(summary.started_at)
        self.assertFalse(summary.native["window_verified"])
        self.assertEqual(summary.native["time_origin_ms"], 1788817932273.3235)
        self.assertEqual(summary.native["duration_ms"], 90000)

    def test_explicit_uuid_link_works_across_remote_and_sdk_session_ids(self):
        state = {}
        self.parse(user_record(), state)
        summary = self.parse(result_record(user_message_uuid="user-1"), state).summaries[0]
        self.assertEqual(summary.started_at, "2026-09-07T22:15:00.000Z")
        self.assertEqual(summary.native["boundary_method"], "linked_user_message")
        self.assertTrue(summary.native["scope_verified"])
        self.assertTrue(summary.native["window_verified"])
        self.assertNotIn("PRIVATE", json.dumps(state))
        self.parse(user_record(created_at="2026-09-07T22:16:00Z"), state)
        repeated = self.parse(result_record(user_message_uuid="user-1"), state).summaries[0]
        self.assertEqual(repeated.started_at, summary.started_at)

    def test_all_linked_users_must_exist_and_precede_result(self):
        state = {}
        self.parse(user_record(), state)
        summary = self.parse(result_record(user_message_uuid="user-1", user_message_uuids=["user-1", "missing"]), state).summaries[0]
        self.assertIsNone(summary.started_at)
        self.assertFalse(summary.native["window_verified"])
        self.parse(user_record(uuid="future-user", created_at="2026-09-08T00:00:00Z"), state)
        self.assertIsNone(self.parse(result_record(user_message_uuid="future-user"), state).summaries[0].started_at)

    def test_summary_identity_is_stable_across_copies_and_count_revisions(self):
        first = self.parse(result_record()).summaries[0]
        changed = result_record(timestamp="2026-09-07T22:17:08Z")
        changed["usage"]["output_tokens"] = 80
        second = self.parse(changed).summaries[0]
        self.assertEqual(first.summary_key, second.summary_key)
        self.assertNotEqual(first.usage, second.usage)
        changed["session_id"] = "different-session"
        self.assertNotEqual(first.summary_key, self.parse(changed).summaries[0].summary_key)

    def test_unknown_models_and_invalid_counts_remain_uncertain(self):
        record = result_record()
        record["usage"].update(input_tokens=True, output_tokens="50", cache_read_input_tokens=-2)
        record["modelUsage"]["new-provider-model"] = {"inputTokens": 1.5, "outputTokens": 7, "private": "PRIVATE_MODEL_TEXT"}
        summary = self.parse(record).summaries[0]
        self.assertIsNone(summary.usage["input_tokens"])
        self.assertIsNone(summary.usage["output_tokens"])
        self.assertIsNone(summary.usage["cache_read_input_tokens"])
        self.assertEqual(summary.model_usage["new-provider-model"], {"input_tokens": None, "output_tokens": 7})
        self.assertNotIn("PRIVATE", json.dumps(asdict(summary)))

    def test_error_or_unproven_completion_cannot_be_terminal(self):
        for extra in ({"is_error": True}, {"subtype": "error_max_budget_usd"}, {"terminal_reason": None}, {"is_error": None}):
            with self.subTest(extra=extra):
                self.assertFalse(self.parse(result_record(**extra)).summaries[0].terminal)

    def test_subagent_result_cannot_claim_verified_main_agent_scope(self):
        state = {}
        self.parse(user_record(), state)
        summary = self.parse(result_record(user_message_uuid="user-1", parent_tool_use_id="tool-child"), state).summaries[0]
        self.assertFalse(summary.native["scope_verified"])
        self.assertIn("subagent_summary", summary.flags)

    def test_direct_sidechain_and_agent_id_block_verified_main_agent_scope(self):
        for markers in ({"isSidechain": True}, {"agentId": "child-agent"}):
            with self.subTest(markers=markers):
                state = {}
                self.parse(user_record(), state)
                summary = self.parse(result_record(user_message_uuid="user-1", **markers), state).summaries[0]
                self.assertFalse(summary.native["scope_verified"])
                self.assertFalse(summary.native["window_verified"])
                self.assertNotIn("boundary_method", summary.native)
                self.assertIn("subagent_summary", summary.flags)
                if "agentId" in markers:
                    self.assertEqual(summary.native["agent_id"], "child-agent")
                else:
                    self.assertTrue(summary.native["is_sidechain"])

    def test_agent_progress_result_preserves_enclosing_subagent_markers(self):
        state = {}
        self.parse(user_record(), state)
        wrapped = {"type": "progress", "session_id": "sdk-session", "data": {
            "type": "agent_progress", "agentId": "nested-agent", "message":
            result_record(user_message_uuid="user-1", agentId=None, isSidechain=False)}}
        summary = self.parse(wrapped, state).summaries[0]
        self.assertEqual(summary.native["agent_id"], "nested-agent")
        self.assertTrue(summary.native["is_sidechain"])
        self.assertFalse(summary.native["scope_verified"])
        self.assertFalse(summary.native["window_verified"])
        self.assertIn("subagent_summary", summary.flags)

    def test_result_without_stable_id_fails_closed(self):
        record = result_record()
        del record["uuid"]
        parsed = self.parse(record)
        self.assertFalse(parsed.summaries)
        self.assertTrue(parsed.warnings)

    def test_quota_created_at_unknown_windows_and_repeat_identity(self):
        parsed = self.parse(quota_record())
        self.assertFalse(parsed.events)
        self.assertFalse(parsed.summaries)
        snapshot = parsed.snapshots[0]
        self.assertEqual(snapshot.timestamp, "2026-09-07T22:14:17.933Z")
        windows = {window["name"]: window for window in snapshot.data["windows"]}
        self.assertEqual(windows["five_hour"]["used_percent"], 22)
        self.assertEqual(windows["five_hour"]["window_minutes"], 300)
        self.assertEqual(windows["seven_day_fable"]["used_percent"], 76)
        self.assertIsNone(windows["seven_day_fable"]["window_minutes"])
        self.assertIsNone(windows["seven_day_fable"]["resets_at"])
        self.assertIsNone(windows["future_pool"]["used_percent"])
        self.assertNotIn("PRIVATE", json.dumps(asdict(snapshot)))
        self.assertEqual(snapshot.snapshot_key, self.parse(deepcopy(quota_record())).snapshots[0].snapshot_key)

    def test_quota_legacy_content_key_stays_stable_and_corrections_are_history(self):
        record = {"type": "rate_limit_event", "uuid": "known-quota", "timestamp": "2026-09-07T10:00:00Z",
                  "rate_limit_info": {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": .25}}}}
        legacy_data = {"windows": [{"name": "five_hour", "window_minutes": 300,
                       "used_percent": 25.0, "native_utilization": .25, "resets_at": None}],
                       "source": "local_rate_limit_event", "status": "allowed"}
        snapshot = self.parse(record).snapshots[0]
        self.assertEqual(snapshot.snapshot_key, stable_key("anthropic", "quota", "2026-09-07T10:00:00.000Z", legacy_data))
        record["rate_limit_info"]["unifiedWindows"]["five_hour"]["utilization"] = .5
        self.assertNotEqual(snapshot.snapshot_key, self.parse(record).snapshots[0].snapshot_key)

    def test_cache_snapshot_groups_do_not_share_user_link_state(self):
        def window(*records):
            return {"format": "claude_cache_metadata_v1", "recognized": True, "history_complete": False, "records": list(records)}
        grouped = {"format": "claude_cache_metadata_v1", "snapshots": [
            window(user_record()), window(result_record(user_message_uuid="user-1"), quota_record())]}
        parsed = parse_metadata(grouped, source_path="/cache/file")
        self.assertEqual(len(parsed.summaries), 1)
        self.assertEqual(len(parsed.snapshots), 1)
        self.assertIsNone(parsed.summaries[0].started_at)
        self.assertEqual(parsed.summaries[0].native["source_kind"], "desktop_conversation_cache")
        self.assertIn("cache_partial_history", parsed.summaries[0].flags)

    @unittest.skipUnless(find_node(), "Optional Node.js decoder not installed")
    def test_binary_cache_summary_quota_and_user_metadata_never_emit_bodies(self):
        records = [user_record(), result_record(user_message_uuid="user-1"), quota_record()]
        value = {"product": "cowork", "tree": {"kind": "cowork_remote", "hasOlder": True,
                 "events": [{"kind": "message", "payload": record} for record in records]}}
        script = "const fs=require('node:fs'),v8=require('node:v8');const o=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(Buffer.concat([Buffer.from([255,21,254]),Buffer.alloc(12),v8.serialize(o)]));"
        binary = subprocess.run([find_node(), "-e", script], input=json.dumps(value).encode(), capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blob"
            path.write_bytes(binary)
            helper = Path(__file__).parents[1] / "usage_tracker/collectors/claude_cache_decode.js"
            output = subprocess.run([find_node(), str(helper), str(path)], capture_output=True, check=True).stdout
            self.assertNotIn(b"PRIVATE", output)
            parsed = parse_cache(path)
        self.assertFalse(parsed.events)
        self.assertEqual(len(parsed.summaries), 1)
        self.assertEqual(len(parsed.snapshots), 1)
        self.assertTrue(parsed.summaries[0].native["window_verified"])
        self.assertEqual(parsed.summaries[0].started_at, "2026-09-07T22:15:00.000Z")


if __name__ == "__main__":
    unittest.main()
