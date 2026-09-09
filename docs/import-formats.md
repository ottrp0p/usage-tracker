# Local import formats

Chat imports are **visible-text estimates**, not reported or billed tokens.
The importer computes `ceil(total UTF-8 bytes in a message / 4)` once per message
and labels every record `estimated` with method `visible_text_utf8_heuristic`.
It does not use a tokenizer. Accuracy varies by language, code, and model.
Empty or media-only messages have a zero visible-text estimate with explicit
omission flags; this does not mean their actual usage was zero.

Context replay, hidden instructions, hidden reasoning, tool activity, attachments,
images, and audio usage cannot be recovered. Imports retain counts, IDs, dates,
model evidence, and limitation flags. Message bodies, conversation titles,
attachment names, and complete source payloads are never returned for storage.
The original JSON or ZIP is read locally without modification or extraction.

## ChatGPT

Supported JSON is a list of conversation objects with an `id` (or
`conversation_id`) and a `mapping` object. A single conversation object or a
`{"conversations": [...]}` wrapper is also accepted. Mapping nodes contain
`message` objects with `author.role`, `content.content_type`, `content.parts`,
and optional `create_time` and `metadata.model_slug`.

**Branch policy:** count every distinct exported user and assistant message from
all branches, including regenerated alternatives. The selected `current_node`
does not restrict the import. Each event carries `all_exported_branches_included`.
This is a history-of-exported-text measure, not the currently selected transcript.

System/tool messages, hidden messages, analysis-channel messages, and messages
addressed to tools are skipped. For `text` and `multimodal_text`, only text parts
are estimated. Unsupported media is omitted with a limitation flag.

`message.id` identifies a message; its mapping node ID is the fallback.
Conversation and message IDs must be stable nonempty strings. Repeated imports,
renamed/copied exports, and edited text with the same IDs use the same event key,
so persistence replaces the estimate instead of adding it again. The last
appearance of an ID within one import wins. Source exports are treated as
additive history: deletion from a newer export does not delete an old stored row.

[OpenAI's export instructions](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data)
describe obtaining an account export. These instructions are not an official
schema guarantee; unrecognized future structures fail explicitly.

## Claude

Supported JSON is a list of conversation objects with a `uuid` (or `id`) and
`chat_messages`. Single objects and the `conversations` wrapper are accepted.
Messages need `uuid` (or `id`), `sender` (`human`/`user` or `assistant`), and text
in either `text` or `content` text blocks. If both text representations exist,
structured text is counted once. `created_at` supplies the message timestamp;
`model`, when explicitly present on the message, supplies the model name.
Tools and non-text blocks are omitted, and attachments generate an omission flag.
The same stable-ID update behavior applies as for ChatGPT.

[Claude's export instructions](https://support.claude.com/en/articles/9450526-export-your-claude-data)
describe obtaining user information and chat history. This importer accepts the
shapes above and does not claim complete coverage of future export revisions.

## JSON, ZIP, and unknown dates

Use provider `auto`, `openai`/`chatgpt`, or `anthropic`/`claude`. Provider inference
uses the conversation structure. Explicit provider mismatches, mixed providers,
unsupported shapes, and missing stable IDs fail before any result is persisted.
An empty export requires an explicit provider and reports zero imported messages.

ZIP imports read only files named `conversations.json` or numbered
`conversations-NNN.json` shards, including nested paths. Other archive members
are ignored. Nothing is extracted. Files are limited to 512 MiB each and selected
archive members to 1 GiB total uncompressed; these limits bound memory exposure.

Missing or malformed message timestamps stay `null`/unknown. File dates,
conversation dates, and import time are not used to invent message dates.

## Account snapshot interchange

An explicitly imported JSON account snapshot is separate from session usage and
always has `scope: account_scope_unverified`. It does not establish provenance,
freshness, billing scope, or completeness, and is never added to session totals.
This is a small local interchange format, not an automatic account API importer.

Example:

```json
{
  "timestamp": "2026-09-07T12:00:00Z",
  "usage": {"input_tokens": 1000, "output_tokens": 500},
  "quota": {"primary": {"used_percent": 25, "window_minutes": 300}}
}
```

`timestamp` or `captured_at` is optional and accepts ISO time or Unix seconds.
Only nonnegative finite JSON numbers are retained; booleans and numeric strings
are rejected as counts. The numeric field allowlist is:

- `input_tokens`, `output_tokens`, `total_tokens`, `cached_input_tokens`,
  `cache_creation_tokens`, `reasoning_tokens`
- `requests`, `messages`, `limit`, `used`, `remaining`, `used_percent`,
  `remaining_percent`, `window_minutes`

Fields may be at the root or within two levels of `usage`, `quota`, `summary`,
`primary`, and `secondary` objects. All other fields and arbitrary text are
discarded. A file with no accepted numeric evidence fails explicitly.
