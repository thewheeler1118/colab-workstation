# Issue draft: P2: Drive API download benchmark versus DriveFS

STATUS: PROPOSED_ONLY — needs a separately authorised live run; never run from CI.

## Context

drive_depot.py / drive_api.py (Drive v3 API path); h3_remote_worker.restore_weights (DriveFS path). Historical data: 40GB restore at 51.26 MB/s (A run h3-20261112T090027Z-8b07c6); mount acceptance first-256MB at 44.9 MB/s.

## Reproduction

1. On a live GPU session (separately authorised), time a 40GB restore via DriveFS and via Drive API direct download under the same session budget.
2. Record MB/s, CU consumed, and failure modes for each path.

## Acceptance

Acceptance: a benchmark note in docs/research/ records both paths' measured throughput and CU cost with run ids; no code change is required unless one path is decisively better. Expected impact: restore-path choice becomes evidence-based instead of habitual.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
