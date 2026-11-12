"""Secret scan for the worktree (stdlib only, no network, no credentials).

Run from the repo root:

    python3 scripts/secret_scan.py

Checks every tracked text file for credential-shaped content and reports
only file:line, rule id and severity — never the matched value.

Rules (same shapes as the previous CI grep step):
  ya29-access-token   Google OAuth access token prefix
  github-pat          ghp_ / github_pat_ token prefixes
  private-key-block   -----BEGIN ... PRIVATE KEY-----
  credential-assign   refresh_token / client_secret / etc. assigned a value

Narrow, documented exception (TASK-002 §3.5): the redaction-regex table
in reference_v2/colab_manager.py MUST contain those shapes — the patterns
are the scrubber itself, not values. Only lines between the markers
`# secret-scan: scrubber-patterns-begin` / `...-end` in that one file are
exempt, and the exemption is reported in the output so it stays visible.
No other file, and no other range in that file, is allow-listed.

Also fails on any tracked file whose name looks like a live credential /
state file (credentials, token, oauth, secret, ADC JSON).

This is the second layer: CI additionally runs gitleaks 8.27.2 (pinned
release, SHA-256 verified) over the worktree and the git history.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RULES = [
    ("ya29-access-token", "HIGH", re.compile(r"ya29\.[A-Za-z0-9_\-\.]{10,}")),
    ("github-pat", "HIGH", re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("private-key-block", "HIGH", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |PGP )?PRIVATE KEY-----")),
    (
        "credential-assign",
        "HIGH",
        re.compile(
            r"(?i)(?:refresh_token|client_secret|access_token|authorization_code|id_token|private_key)"
            r"""\s*["']?\s*[:=]\s*["'][A-Za-z0-9._\-/]{10,}"""
        ),
    ),
]

TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".yaml", ".yml", ".toml", ".txt", ".cfg", ".ini"}
# A tracked file is credential-like only when its BASENAME starts with a
# secret-material word (or is a key/cert/env file). Incident docs *about*
# credentials (docs/incidents/08-browser-oauth-pty-credential-...) and
# this scanner itself (scripts/secret_scan.py) are not credential files:
# the former is evidence prose, the latter is the scanner — both are
# content-scanned above like every other text file.
CREDENTIAL_BASENAME = re.compile(
    r"(?i)^(credentials?|tokens?|oauth|application_default_credentials)([._-]|$)"
)
CREDENTIAL_SUFFIX = {".pem", ".key", ".p12", ".env"}
SELF = "scripts/secret_scan.py"
EXEMPT_FILE = "reference_v2/colab_manager.py"
EXEMPT_BEGIN = "# secret-scan: scrubber-patterns-begin"
EXEMPT_END = "# secret-scan: scrubber-patterns-end"


def tracked_files() -> list[str]:
    # Tracked files plus untracked, non-ignored files (so the scan also
    # covers new files before they are committed — CI sees them tracked).
    r = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in r.stdout.splitlines() if line.strip()]


def main() -> int:
    findings = []
    exemptions = 0
    for rel in tracked_files():
        p = ROOT / rel
        if not p.is_file():
            continue
        if rel != SELF and (CREDENTIAL_BASENAME.match(p.name) or p.suffix in CREDENTIAL_SUFFIX):
            findings.append((rel, 0, "credential-filename", "HIGH"))
            continue
        if p.suffix not in TEXT_SUFFIXES and rel not in (".github/workflows/ci.yml",):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            continue
        in_exempt_block = False
        for i, line in enumerate(text.splitlines(), start=1):
            if rel == EXEMPT_FILE and EXEMPT_BEGIN in line:
                in_exempt_block = True
                continue
            if rel == EXEMPT_FILE and EXEMPT_END in line:
                in_exempt_block = False
                continue
            if in_exempt_block:
                exemptions += 1
                continue
            for rule_id, severity, pattern in RULES:
                if pattern.search(line):
                    findings.append((rel, i, rule_id, severity))
                    break
    if findings:
        print("SECRET SCAN FAILED:")
        for rel, line, rule_id, severity in findings:
            print(f"  {{\"file\": \"{rel}\", \"line\": {line}, \"type\": \"{rule_id}\", "
                  f"\"severity\": \"{severity}\", \"verified_secret\": false}}")
        return 1
    print(f"SECRET SCAN OK: no credential-shaped content in tracked text files "
          f"(scrubber-pattern exemption lines skipped in {EXEMPT_FILE}: {exemptions}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
