# Audits — third-party claims kept separate

This directory holds the two vendored patch write-ups (live-verified by Muse)
and the historical P0 prompt docs. It does NOT contain a merged third-party audit.

- `v3_cli_proxy_patch.md` — empty-POST 411 workaround (verified live 2026-11-11).
- `v3_drivemount_timeout_patch.md` — drivemount timeout_ms=600000 (mock-verified; live mounts succeeded but auth stayed inside the old 120s window — see incidents/07).
- `archive_p0_code_level_prompt.md`, `archive_p0_readme.md` — historical P0 prompt docs, kept for provenance only.

Rule (TASK-001 §5): never upgrade a third-party audit judgment into a fact. Audit claims live here or in docs/issues/ as PROPOSED_ONLY until Muse verifies them with its own runs and tests.
