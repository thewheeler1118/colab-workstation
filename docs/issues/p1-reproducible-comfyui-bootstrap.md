# Issue draft: P1: Reproducible ComfyUI environment bootstrap

STATUS: PROPOSED_ONLY.

## Context

h3_comfyui_cache.py; h3_remote_worker.py — ensure_comfy / restore_comfyui_from_cache / clone_comfyui_atomic. Historical runs: h3-20261112T090027Z-8b07c6 (cache restore OK); 2026-11-12 git clone exit 128 in ensure_comfy.

## Reproduction

1. From a clean remote dir, run ensure_comfy twice: once with the Drive source cache present, once with only git available; assert the pinned commit 61e524bdba7a4e193b45c2697f1af83c1beb86d6 in both cases.
2. Corrupt the cache tarball sha and assert fail-closed (no silent fallback to an unpinned tree).

## Acceptance

Acceptance: tests/test_h3_comfyui_cache.py grows a case proving cache-corruption fails closed; a doc records the exact bootstrap order (cache → verify sha → extract → verify commit → clone fallback). Expected impact: ComfyUI bootstrap is deterministic and auditable instead of network-dependent.

## Process

Issue → feature branch → tests → pull request → review → merge. Never commit directly to main.
