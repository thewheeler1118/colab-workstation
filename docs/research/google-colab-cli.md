# Research note: google-colab-cli (design reference only)

- https://github.com/googlecolab/google-colab-cli
- Pinned version in production: 0.7.5 (see requirements.lock).

The official CLI is the ONLY GPU channel. Two vendored patches against 0.7.5 live in patches/google-colab-cli-0.7.5/ as real unified diffs against the pristine wheel, with fail-closed preflight gates in h3_controller.py. No third-party DFS extension, no Mac relay, no other cloud. The Drive v3 remote-download design discussion is kept in drive_v3_remote_download_design.md (historical design doc from the source project, not a verified production path).
