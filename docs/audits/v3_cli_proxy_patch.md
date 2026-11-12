# Vendored Colab CLI transport patch — POST body workaround (2026-11-11)

## File patched
`.venv/lib/python3.12/site-packages/colab_cli/client.py`,
`Client._issue_request()`, in the official `google-colab-cli==0.7.5`.

## Exact change
```python
# VM transport workaround (2026-11-11, verified live): this VM's
# egress proxy strips Content-Length: 0 from empty POSTs, and
# Google's frontend then returns 411 Length Required for
# /tun/m/assign and /tun/m/unassign. Sending a 2-byte body gives
# Content-Length: 2, which the proxy preserves; the Colab backend
# ignores the body (verified live: assign 200, unassign 204).
if method == "POST" and "data" not in kwargs and "json" not in kwargs:
    kwargs["data"] = b"{}"
```

## Why it exists (live evidence, 2026-11-11)
- `colab new --gpu G4` failed 4/4 at POST `/tun/m/assign`:
  ProxyError / RemoteDisconnected ×3, ChunkedEncodingError
  IncompleteRead(1025 bytes read, 539 more expected) ×1.
- Isolation: GET `/tun/m/ccu-info`, `/tun/m/assignments` and
  `/tun/m/assign` (token) all succeeded 9/9. Empty POSTs returned
  411 Length Required with a 1564-byte HTML page (full body when not
  truncated). requests locally sends `Content-Length: 0`
  (PreparedRequest verified), so the proxy strips it in transit —
  the same proxy behaviour as the 2026-11-10 Drive empty-PUT 411.
- With a 2-byte body: invalid token → 400 (reaches Colab logic),
  valid G4 token → 503 (3/3, 30.3s, G4-side), valid A100 token →
  200 and allocation in 2.7s, unassign → 204.
- The ~1025-byte truncation is intermittent and independent: it
  masks the 411 as a transport error. The controller retries the
  same GPU once on transport errors (`is_transient_transport_error`)
  and never falls back to another GPU on transport alone.

## Scope and limits
- Only POSTs without an explicit body are affected; GETs and POSTs
  with a body are untouched. The Colab backend ignores the `{}` body.
- This patches the venv copy only. If the venv is rebuilt or the CLI
  is upgraded, the patch is lost — `h3_controller.preflight` checks
  for it (`cli_post_body_patch`) and fails closed before any GPU is
  requested, and `tests/test_h3_production.py` asserts its presence.
- No architecture change: the official CLI binary remains the only
  GPU channel; no third-party DFS, no Mac relay, no other cloud.
