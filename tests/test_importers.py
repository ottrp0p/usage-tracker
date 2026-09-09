"""Exercise estimates, identity, export branching, and privacy at the boundary."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from usage_tracker.importers import ImportFormatError, import_account_snapshot, import_export


def chatgpt_message(identity, role, text, **extra):
    return {"id": identity, "author": {"role": role}, "create_time": 1_700_000_000, "content": {"content_type": "text", "parts": [text]}, **extra}


def chatgpt_conversation():
    return {
        "id": "conversation-cg",
        "title": "PRIVATE TITLE DO NOT SAVE",
        "current_node": "answer-two",
        "mapping": {
            "root": {"message": None, "parent": None},
            "question": {"parent": "root", "message": chatgpt_message("question", "user", "hello")},
            "answer-one": {"parent": "question", "message": chatgpt_message("answer-one", "assistant", "first answer", metadata={"model_slug": "gpt-4o"})},
            "answer-two": {"parent": "question", "message": chatgpt_message("answer-two", "assistant", "second answer")},
        },
    }


def claude_conversation():
    return {
        "uuid": "conversation-cl",
        "name": "PRIVATE CLAUDE TITLE",
        "chat_messages": [
            {"uuid": "human-one", "sender": "human", "text": "hello", "created_at": "2026-09-01T12:00:00Z"},
            {"uuid": "assistant-one", "sender": "assistant", "text": "world", "created_at": "2026-09-01T12:00:02+00:00"},
        ],
    }


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "conversations.json"

    def write(self, document):
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return self.path

    def test_chatgpt_estimates_all_distinct_exported_branches(self):
        result = import_export(self.write([chatgpt_conversation()]))
        self.assertEqual(3, len(result.events))
        self.assertEqual([2, 3, 4], [event.total_tokens for event in result.events])
        self.assertEqual(2, result.events[0].input_tokens)
        self.assertEqual(3, result.events[1].output_tokens)
        self.assertEqual("gpt-4o", result.events[1].model)
        for event in result.events:
            self.assertEqual("openai", event.provider)
            self.assertEqual("chatgpt", event.surface)
            self.assertEqual("estimated", event.quality)
            self.assertEqual("visible_text_utf8_heuristic", event.method)
            self.assertIn("all_exported_branches_included", event.flags)
            self.assertIsNone(event.cached_input_tokens)
        self.assertTrue(any("all exported branches" in warning for warning in result.warnings))

    def test_utf8_rounding_once_per_message_not_characters(self):
        conversation = chatgpt_conversation()
        message = conversation["mapping"]["question"]["message"]
        message["content"]["parts"] = ["😀", "é"]  # Six bytes; two characters.
        event = import_export(self.write(conversation)).events[0]
        self.assertEqual(2, event.total_tokens)
        self.assertEqual({"visible_text_utf8_bytes": 6, "visible_text_characters": 2, "text_part_count": 2}, event.native)

    def test_multiple_parts_do_not_independently_round_or_add_separators(self):
        conversation = chatgpt_conversation()
        conversation["mapping"]["question"]["message"]["content"]["parts"] = ["a", "b", "c", "d"]
        event = import_export(self.write(conversation)).events[0]
        self.assertEqual(1, event.input_tokens)

    def test_repeat_export_and_revision_have_same_key(self):
        conversation = chatgpt_conversation()
        first = import_export(self.write([conversation])).events[0]
        conversation["title"] = "a changed title"
        conversation["mapping"]["question"]["message"]["content"]["parts"] = ["This message is now longer."]
        revised = import_export(self.write([conversation])).events[0]
        self.assertEqual(first.event_key, revised.event_key)
        self.assertNotEqual(first.total_tokens, revised.total_tokens)
        copied_path = Path(self.directory.name) / "copied.json"
        copied_path.write_bytes(self.path.read_bytes())
        self.assertEqual(revised.event_key, import_export(copied_path).events[0].event_key)

    def test_duplicate_ids_inside_one_document_collapse(self):
        conversation = claude_conversation()
        conversation["chat_messages"].append(dict(conversation["chat_messages"][0], text="revised text"))
        result = import_export(self.write([conversation, conversation]))
        self.assertEqual(2, len(result.events))
        self.assertEqual(3, result.events[0].input_tokens)

    def test_missing_timestamp_stays_unknown_despite_conversation_date(self):
        conversation = chatgpt_conversation()
        conversation["create_time"] = 1_600_000_000
        del conversation["mapping"]["question"]["message"]["create_time"]
        event = import_export(self.write(conversation)).events[0]
        self.assertIsNone(event.timestamp)
        self.assertIn("timestamp_missing", event.flags)

    def test_zero_timestamp_is_valid(self):
        conversation = chatgpt_conversation()
        conversation["mapping"]["question"]["message"]["create_time"] = 0
        self.assertEqual("1970-01-01T00:00:00.000Z", import_export(self.write(conversation)).events[0].timestamp)

    def test_system_tools_hidden_and_analysis_are_excluded(self):
        conversation = chatgpt_conversation()
        mapping = conversation["mapping"]
        for identity, role, extra in [
            ("system", "system", {}),
            ("tool", "tool", {}),
            ("hidden", "assistant", {"metadata": {"is_visually_hidden_from_conversation": True}}),
            ("analysis", "assistant", {"channel": "analysis"}),
            ("tool-call", "assistant", {"recipient": "python"}),
        ]:
            mapping[identity] = {"message": chatgpt_message(identity, role, "SECRET OMITTED", **extra)}
        self.assertEqual(3, len(import_export(self.write(conversation)).events))

    def test_multimodal_preserves_text_only_and_flags_omission(self):
        conversation = chatgpt_conversation()
        conversation["mapping"]["question"]["message"]["content"] = {
            "content_type": "multimodal_text",
            "parts": ["hello", {"content_type": "image_asset_pointer", "asset_pointer": "SECRET IMAGE PATH"}],
        }
        event = import_export(self.write(conversation)).events[0]
        self.assertEqual(2, event.input_tokens)
        self.assertIn("multimodal_or_unsupported_content_omitted", event.flags)
        self.assertNotIn("SECRET", json.dumps(asdict(event)))

    def test_unsupported_media_only_remains_explicit_zero_text_estimate(self):
        conversation = chatgpt_conversation()
        conversation["mapping"]["question"]["message"]["content"] = {"content_type": "audio", "payload": "PRIVATE"}
        event = import_export(self.write(conversation)).events[0]
        self.assertEqual(0, event.total_tokens)
        self.assertIn("no_visible_text", event.flags)
        self.assertIn("multimodal_or_unsupported_content_omitted", event.flags)

    def test_only_metadata_and_counts_are_returned(self):
        result = import_export(self.write(chatgpt_conversation()))
        serialized = json.dumps(asdict(result))
        for private in ["PRIVATE TITLE", "hello", "first answer", "second answer", "content_type", "mapping"]:
            self.assertNotIn(private, serialized)

    def test_claude_legacy_text_export(self):
        result = import_export(self.write([claude_conversation()]), provider="claude")
        self.assertEqual([2, 2], [event.total_tokens for event in result.events])
        self.assertEqual(["anthropic"] * 2, [event.provider for event in result.events])
        self.assertEqual(["claude_chat"] * 2, [event.surface for event in result.events])
        self.assertEqual("2026-09-01T12:00:00.000Z", result.events[0].timestamp)

    def test_claude_structured_text_not_double_counted(self):
        conversation = claude_conversation()
        conversation["chat_messages"][0]["content"] = [{"type": "text", "text": "hello"}, {"type": "image", "source": "PRIVATE"}]
        conversation["chat_messages"][0]["attachments"] = [{"file_name": "PRIVATE"}]
        event = import_export(self.write(conversation)).events[0]
        self.assertEqual(2, event.input_tokens)
        self.assertEqual(5, event.native["visible_text_utf8_bytes"])
        self.assertIn("multimodal_or_unsupported_content_omitted", event.flags)

    def test_claude_thinking_and_tools_omitted(self):
        conversation = claude_conversation()
        conversation["chat_messages"][1]["content"] = [{"type": "thinking", "thinking": "PRIVATE"}, {"type": "text", "text": "world"}]
        conversation["chat_messages"].append({"sender": "tool", "text": "PRIVATE"})
        result = import_export(self.write(conversation))
        self.assertEqual(2, len(result.events))
        self.assertEqual(2, result.events[1].output_tokens)

    def test_zip_reads_nested_shards_without_extraction(self):
        archive_path = Path(self.directory.name) / "export.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("nested/conversations-000.json", json.dumps([chatgpt_conversation()]))
            archive.writestr("../conversations-001.json", json.dumps([chatgpt_conversation()]))
            archive.writestr("user.json", "not even JSON; deliberately unread")
            archive.writestr("../../NEVER-EXTRACT", "private")
        before = set(Path(self.directory.name).iterdir())
        result = import_export(archive_path)
        self.assertEqual(3, len(result.events))
        self.assertEqual(before, set(Path(self.directory.name).iterdir()))

    def test_zip_without_conversations_and_bad_zip_fail(self):
        archive_path = Path(self.directory.name) / "export.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("chat.html", "unsupported")
        with self.assertRaisesRegex(ImportFormatError, "no conversations"):
            import_export(archive_path)
        archive_path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(ImportFormatError, "not a readable ZIP"):
            import_export(archive_path)

    def test_invalid_json_error_has_no_payload(self):
        self.path.write_text("PRIVATE malformed payload", encoding="utf-8")
        with self.assertRaises(ImportFormatError) as raised:
            import_export(self.path)
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_unsupported_or_mixed_formats_fail(self):
        for document in [{"messages": []}, ["bad"], [dict(chatgpt_conversation(), chat_messages=[])], [chatgpt_conversation(), claude_conversation()]]:
            with self.subTest(document_type=type(document).__name__), self.assertRaises(ImportFormatError):
                import_export(self.write(document))
        with self.assertRaisesRegex(ImportFormatError, "does not match"):
            import_export(self.write(chatgpt_conversation()), provider="anthropic")

    def test_empty_export_requires_explicit_provider(self):
        with self.assertRaisesRegex(ImportFormatError, "Empty export"):
            import_export(self.write([]))
        self.assertEqual([], import_export(self.path, "openai").events)

    def test_missing_stable_identity_fails(self):
        conversation = claude_conversation()
        del conversation["chat_messages"][0]["uuid"]
        with self.assertRaisesRegex(ImportFormatError, "stable string ID"):
            import_export(self.write(conversation))
        conversation = chatgpt_conversation()
        del conversation["id"]
        with self.assertRaisesRegex(ImportFormatError, "stable string ID"):
            import_export(self.write(conversation))

    def test_chatgpt_mapping_node_identity_fallback(self):
        conversation = chatgpt_conversation()
        del conversation["mapping"]["question"]["message"]["id"]
        self.assertEqual("question", import_export(self.write(conversation)).events[0].request_id)

    def test_malformed_routing_fields_fail_clearly(self):
        conversation = chatgpt_conversation()
        conversation["mapping"]["question"]["message"]["recipient"] = []
        with self.assertRaisesRegex(ImportFormatError, "recipient"):
            import_export(self.write(conversation))
        conversation = claude_conversation()
        conversation["chat_messages"][0]["sender"] = {}
        with self.assertRaisesRegex(ImportFormatError, "sender"):
            import_export(self.write(conversation))

    def test_account_snapshot_numeric_whitelist_and_separate_scope(self):
        document = {"timestamp": "2026-09-01T12:00:00Z", "scope": "trusted", "name": "PRIVATE", "usage": {"input_tokens": 100, "output_tokens": 20, "text": "PRIVATE"}, "quota": {"primary": {"used_percent": 12.5, "window_minutes": 300, "label": "PRIVATE"}}, "messages": True, "remaining": -1, "limit": "100", "arbitrary_number": 900}
        result = import_account_snapshot(self.write(document), "chatgpt")
        self.assertEqual([], result.events)
        snapshot = result.snapshots[0]
        self.assertEqual("account_scope_unverified", snapshot.scope)
        self.assertEqual({"usage": {"input_tokens": 100, "output_tokens": 20}, "quota": {"primary": {"used_percent": 12.5, "window_minutes": 300}}}, snapshot.data)
        self.assertNotIn("PRIVATE", json.dumps(asdict(snapshot)))
        self.assertEqual(snapshot.snapshot_key, import_account_snapshot(self.path, "openai").snapshots[0].snapshot_key)

    def test_account_rejects_unrecognized_and_nonfinite_numbers(self):
        for document in [[], {"title": "PRIVATE"}, {"used_percent": float("nan"), "remaining": float("inf")}, {"scope": {"total_tokens": 5}}]:
            with self.subTest(document_type=type(document).__name__), self.assertRaises(ImportFormatError):
                import_account_snapshot(self.write(document), "openai")

    def test_account_missing_timestamp_stays_unknown(self):
        result = import_account_snapshot(self.write({"total_tokens": 100}), "claude")
        self.assertIsNone(result.snapshots[0].timestamp)

    def test_account_zero_timestamp_is_valid(self):
        result = import_account_snapshot(self.write({"timestamp": 0, "total_tokens": 100}), "claude")
        self.assertEqual("1970-01-01T00:00:00.000Z", result.snapshots[0].timestamp)


if __name__ == "__main__":
    unittest.main()
