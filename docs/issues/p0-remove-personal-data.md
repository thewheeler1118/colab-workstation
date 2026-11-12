# Issue draft: P0: Remove personal data from repository and Git history

STATUS: PROPOSED_ONLY.

TASK-002 NOTE (2026-11-12): the worktree is redacted — 6 Google-account
identifiers in `docs/v3_progress.md` → `[GOOGLE_ACCOUNT_REDACTED]`, 1
in `h3_comfyui_cache.py` → "the same authorized Google account", macOS
username paths in `reference_v2/README.md` and
`reference_v2/colab_manager.py` → generic paths (branch
`security/privacy-remediation`, commit `2a68953`). Gitleaks 8.27.2:
0 findings on the worktree and on the full history (1 commit) — no
real tokens, keys, or passwords exist anywhere, so no credential
rotation is required. **Git history is NOT cleaned**: the same
identifiers remain in the single historical commit `17075e6`. The
rewrite is blocked until the repo is Private (currently PUBLIC; the
stored token lacks the Administration scope) AND the user confirms the
force-push. Do not claim history cleaned until a fresh clone re-scan
confirms it.

## Context

docs/v3_progress.md (lines 13, 14, 32, 36, 53, 55); h3_comfyui_cache.py
(line 5); reference_v2/README.md (line 27);
reference_v2/colab_manager.py (line 1997). The repo was migrated as
public while the README claimed private. Private only limits future
access; already-public content may have been copied.

## Reproduction

1. Scan the worktree and the full history with Gitleaks and confirm 0
   findings after the redaction.
2. After the repo is Private and the user confirms, rewrite the single
   commit with git-filter-repo, force-push, re-clone, and re-scan all
   history before marking this fixed.
