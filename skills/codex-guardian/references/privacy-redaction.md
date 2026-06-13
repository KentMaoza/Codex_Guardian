# Privacy Redaction

Before sharing Codex Guardian output publicly:

- Replace the home directory with `~`.
- Remove user names, email addresses, organization names, and client names.
- Remove auth tokens, API keys, cookies, bearer tokens, and session tokens.
- Remove raw `conversationId`, `threadId`, UUID-like IDs, and long opaque IDs.
- Do not include raw transcripts from `.codex/sessions`.
- Do not include `.codex/auth.json`.
- Do not include memory files.
- Do not include proprietary source code unless the repository owner approved it.
- Prefer pattern counts and short sanitized excerpts over full logs.

Safe to share:

- Codex version.
- Platform and OS family.
- Error strings.
- Redacted log line excerpts.
- Pattern counts.
- Approximate file sizes.
- Reproduction steps that do not reveal private project content.

Codex Guardian's CLI redacts common tokens and IDs automatically, but review output manually before sharing it outside the local machine.
