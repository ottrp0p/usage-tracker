"""Read local chat exports and retain only explicitly labeled count estimates.

Exports are not API usage logs: context replay, hidden prompts, reasoning, tools,
and media tokens cannot be reconstructed from their visible message text. The
deliberately simple byte heuristic below must never be labeled billed usage.
"""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
import re
import zipfile

from .models import ParseResult, Snapshot, UsageEvent, stable_key, utc_timestamp


class ImportFormatError(ValueError):
    """The source is not a supported, safely identifiable export."""


_PROVIDERS = {"openai": "openai", "chatgpt": "openai", "anthropic": "anthropic", "claude": "anthropic"}
_CONVERSATION_FILE = re.compile(r"conversations(?:-\d+)?\.json\Z", re.IGNORECASE)
_MAX_MEMBER_BYTES = 512 * 1024 * 1024
_MAX_EXPORT_BYTES = 1024 * 1024 * 1024
_METHOD = "visible_text_utf8_heuristic"
_BASE_FLAGS = ["visible_text_only", "not_billed_usage", "tokenizer_not_used", "context_replay_unobserved", "hidden_content_unobserved"]


def _provider(value: str, *, auto: bool = False) -> str:
    if auto and value.lower() == "auto":
        return "auto"
    try:
        return _PROVIDERS[value.lower()]
    except (KeyError, AttributeError) as exc:
        raise ImportFormatError("Provider must be auto, openai/chatgpt, or anthropic/claude.") from exc


def _decode_json(data: bytes) -> object:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        # Do not put source snippets, titles, or message content into diagnostics.
        raise ImportFormatError("The source is not valid UTF-8 JSON.") from exc


def _documents(path: Path) -> list[object]:
    """Only read conversation JSON members; never extract archive paths to disk."""
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                members = [item for item in archive.infolist() if not item.is_dir() and _CONVERSATION_FILE.fullmatch(PurePosixPath(item.filename).name)]
                if not members:
                    raise ImportFormatError("ZIP contains no conversations.json or conversations-NNN.json.")
                if any(item.file_size > _MAX_MEMBER_BYTES for item in members) or sum(item.file_size for item in members) > _MAX_EXPORT_BYTES:
                    raise ImportFormatError("Conversation JSON exceeds the import size limit (512 MiB/member, 1 GiB/archive).")
                # Sorting gives deterministic revision precedence within an archive.
                return [_decode_json(archive.read(item)) for item in sorted(members, key=lambda item: item.filename)]
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise ImportFormatError("ZIP is damaged, encrypted, or uses unsupported compression.") from exc
    if path.suffix.lower() == ".zip":
        raise ImportFormatError("The source is not a readable ZIP archive.")
    if path.stat().st_size > _MAX_MEMBER_BYTES:
        raise ImportFormatError("JSON exceeds the 512 MiB import size limit.")
    return [_decode_json(path.read_bytes())]


def _conversations(document: object) -> list[dict]:
    if isinstance(document, dict):
        if "mapping" in document or "chat_messages" in document:
            document = [document]
        elif "conversations" in document:
            document = document["conversations"]
    if not isinstance(document, list) or any(not isinstance(item, dict) for item in document):
        raise ImportFormatError("Expected a conversation list, conversations wrapper, or single conversation object.")
    return document


def _identity(value: object, what: str) -> str:
    # Path, title, body hash, and list position are not safe identities: each can
    # change across exports/revisions and silently turn an update into new usage.
    if not isinstance(value, str) or not value.strip():
        raise ImportFormatError(f"{what} has no stable string ID; refusing non-idempotent import.")
    return value


def _chatgpt_parts(message: dict) -> tuple[list[str], list[str]]:
    content = message.get("content")
    if not isinstance(content, dict):
        return [], ["unsupported_content_omitted"]
    content_type = content.get("content_type")
    if not isinstance(content_type, str) or content_type not in {"text", "multimodal_text"}:
        return [], ["multimodal_or_unsupported_content_omitted"]
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        return [], ["unsupported_content_omitted"]
    text, flags = [], []
    for part in parts:
        if isinstance(part, str):
            text.append(part)
        elif isinstance(part, dict) and part.get("type", part.get("content_type")) == "text" and isinstance(part.get("text"), str):
            text.append(part["text"])
        else:
            flags.append("multimodal_or_unsupported_content_omitted")
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and any(metadata.get(key) for key in ("attachments", "audio", "images")):
        flags.append("multimodal_or_unsupported_content_omitted")
    return text, flags


def _claude_parts(message: dict) -> tuple[list[str], list[str]]:
    text, flags = [], []
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            else:
                flags.append("multimodal_or_unsupported_content_omitted")
    elif content is not None:
        flags.append("unsupported_content_omitted")
    # Claude may repeat the same visible text in `text` and `content`. Prefer
    # structured text when present instead of counting both representations.
    if not text and isinstance(message.get("text"), str):
        text = [message["text"]]
    if any(message.get(key) for key in ("attachments", "files", "files_v2")):
        flags.append("multimodal_or_unsupported_content_omitted")
    return text, flags


def _event(provider: str, session_id: str, message_id: str, role: str, timestamp: object, model: object, parts: list[str], flags: list[str]) -> UsageEvent:
    # Sum original bytes without inventing separators; round once per message.
    byte_count = sum(len(part.encode("utf-8")) for part in parts)
    estimate = (byte_count + 3) // 4
    normalized_time = utc_timestamp(timestamp)
    if normalized_time is None:
        flags.append("timestamp_missing")
    if not byte_count:
        flags.append("no_visible_text")
    # Keep unknown model evidence unknown; a subscription/account name is not a
    # reliable model designation and is never inferred from conversation titles.
    model = model if isinstance(model, str) and model else "unknown"
    return UsageEvent(
        event_key=stable_key("chat_export_message", provider, session_id, message_id),
        provider=provider,
        surface="chatgpt" if provider == "openai" else "claude_chat",
        session_id=session_id,
        request_id=message_id,
        timestamp=normalized_time,
        model=model,
        quality="estimated",
        method=_METHOD,
        input_tokens=estimate if role == "user" else 0,
        output_tokens=estimate if role == "assistant" else 0,
        total_tokens=estimate,
        native={"visible_text_utf8_bytes": byte_count, "visible_text_characters": sum(map(len, parts)), "text_part_count": len(parts)},
        flags=sorted(set(_BASE_FLAGS + flags)),
    )


def _chatgpt(conversation: dict) -> list[UsageEvent]:
    session_id = _identity(conversation.get("id") or conversation.get("conversation_id"), "ChatGPT conversation")
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        raise ImportFormatError("ChatGPT conversation mapping must be an object.")
    events = []
    # Include each stored branch's distinct messages. Following current_node
    # would require replacing previous branch rows when a later export changes
    # the selected branch; additive stable-ID imports cannot safely do that.
    for node_id, node in mapping.items():
        if not isinstance(node, dict):
            raise ImportFormatError("ChatGPT mapping nodes must be objects.")
        message = node.get("message")
        if message is None:
            continue  # The synthetic tree root commonly has no message.
        if not isinstance(message, dict):
            raise ImportFormatError("ChatGPT message must be an object.")
        author = message.get("author")
        role = author.get("role") if isinstance(author, dict) else None
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        if not isinstance(role, str) or role not in {"user", "assistant"} or metadata.get("is_visually_hidden_from_conversation"):
            continue
        channel, recipient = message.get("channel"), message.get("recipient")
        if channel is not None and not isinstance(channel, str) or recipient is not None and not isinstance(recipient, str):
            raise ImportFormatError("ChatGPT channel and recipient must be strings or null.")
        if channel in {"analysis", "justify", "confidence"} or recipient not in {None, "all", "user"}:
            continue
        message_id = _identity(message.get("id") or node_id, "ChatGPT message")
        parts, flags = _chatgpt_parts(message)
        flags.append("all_exported_branches_included")
        events.append(_event("openai", session_id, message_id, role, message.get("create_time"), metadata.get("model_slug"), parts, flags))
    return events


def _claude(conversation: dict) -> list[UsageEvent]:
    session_id = _identity(conversation.get("uuid") or conversation.get("id"), "Claude conversation")
    messages = conversation.get("chat_messages")
    if not isinstance(messages, list):
        raise ImportFormatError("Claude chat_messages must be a list.")
    events = []
    for message in messages:
        if not isinstance(message, dict):
            raise ImportFormatError("Claude messages must be objects.")
        sender = message.get("sender")
        if sender is not None and not isinstance(sender, str):
            raise ImportFormatError("Claude message sender must be a string.")
        role = {"human": "user", "user": "user", "assistant": "assistant"}.get(sender)
        if role is None:
            continue
        message_id = _identity(message.get("uuid") or message.get("id"), "Claude message")
        parts, flags = _claude_parts(message)
        events.append(_event("anthropic", session_id, message_id, role, message.get("created_at"), message.get("model"), parts, flags))
    return events


def import_export(path: Path, provider: str = "auto") -> ParseResult:
    """Import a recognized export; repeated message IDs upsert even after edits.

    Missing message dates remain unknown. The source's file modification time or
    conversation modification time would misattribute old messages to new days.
    """
    expected = _provider(provider, auto=True)
    events: dict[str, UsageEvent] = {}
    found_provider = None
    for document in _documents(Path(path)):
        for conversation in _conversations(document):
            detected = "openai" if "mapping" in conversation else "anthropic" if "chat_messages" in conversation else None
            if detected is None or ("mapping" in conversation and "chat_messages" in conversation):
                raise ImportFormatError("Unsupported or ambiguous conversation format; expected ChatGPT mapping or Claude chat_messages.")
            if expected != "auto" and detected != expected:
                raise ImportFormatError("Conversation format does not match the requested provider.")
            if found_provider is not None and detected != found_provider:
                raise ImportFormatError("Mixed providers in a single export are unsupported.")
            found_provider = detected
            for event in (_chatgpt(conversation) if detected == "openai" else _claude(conversation)):
                events[event.event_key] = event
    if found_provider is None and expected == "auto":
        raise ImportFormatError("Empty export cannot identify a provider; pass an explicit provider.")
    warnings = ["Visible-text UTF-8 estimates are not billed tokens; context replay, hidden content, tools, and media usage are unavailable."]
    if found_provider == "openai":
        warnings.append("ChatGPT branch policy: includes distinct messages from all exported branches, not only the currently selected branch.")
    if not events:
        warnings.append("No supported user or assistant messages were found.")
    return ParseResult(events=list(events.values()), warnings=warnings)


# Account summaries are intentionally a small, explicit local interchange
# format, not a recursive copy of arbitrary account exports or billing data.
_SUMMARY_FIELDS = {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "cache_creation_tokens", "reasoning_tokens", "requests", "messages", "limit", "used", "remaining", "used_percent", "remaining_percent", "window_minutes"}
_CONTAINERS = {"usage", "quota", "summary", "primary", "secondary"}


def _summary_numbers(data: dict, depth: int = 0) -> dict:
    output = {}
    for key, value in data.items():
        if key in _SUMMARY_FIELDS and isinstance(value, (int, float)) and not isinstance(value, bool) and (isinstance(value, int) or math.isfinite(value)) and value >= 0:
            output[key] = value
        elif key in _CONTAINERS and isinstance(value, dict) and depth < 2:
            nested = _summary_numbers(value, depth + 1)
            if nested:
                output[key] = nested
    return output


def import_account_snapshot(path: Path, provider: str) -> ParseResult:
    """Retain whitelisted numeric account evidence separately from usage events."""
    normalized_provider = _provider(provider)
    path = Path(path)
    if path.stat().st_size > _MAX_MEMBER_BYTES:
        raise ImportFormatError("Account JSON exceeds the 512 MiB import size limit.")
    document = _decode_json(path.read_bytes())
    if not isinstance(document, dict):
        raise ImportFormatError("Account snapshot must be a JSON object.")
    data = _summary_numbers(document)
    if not data:
        raise ImportFormatError("Account snapshot has no supported nonnegative numeric summary/quota fields.")
    timestamp = utc_timestamp(document.get("timestamp", document.get("captured_at")))
    snapshot = Snapshot(
        snapshot_key=stable_key("imported_account_snapshot", normalized_provider, timestamp, data),
        provider=normalized_provider,
        kind="account_summary",
        timestamp=timestamp,
        data=data,
        scope="account_scope_unverified",
    )
    return ParseResult(snapshots=[snapshot], warnings=["Imported account totals have unverified scope and are never added to local session totals."])
