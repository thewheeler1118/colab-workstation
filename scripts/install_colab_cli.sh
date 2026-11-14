#!/usr/bin/env bash
# install_colab_cli.sh — install the PINNED google-colab-cli 0.7.5 into a
# venv and apply both production patches (TASK-005 §6, Issue #29).
#
# Usage: ./scripts/install_colab_cli.sh [venv_dir]
#   venv_dir defaults to <repo>/.venv (the resolver's canonical location;
#   .venv/ is gitignored). Pass a scratch dir to prove the install without
#   touching the repo venv.
#
# What it does, in order:
#   1. creates the venv if missing (python3 -m venv)
#   2. installs google-colab-cli==0.7.5 from PyPI (official distribution)
#   3. applies patches/google-colab-cli-0.7.5/*.patch with git apply
#      (skips a patch whose marker is already present)
#   4. verifies: `colab version` == 0.7.5, both patch markers present
#
# It never curl|bash anything, never touches the production directory,
# never creates a GPU, and never spends CU. Re-running it is safe: an
# already-patched venv is detected and left alone.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
VENV="${1:-$REPO/.venv}"
PATCH_DIR="$REPO/patches/google-colab-cli-0.7.5"

if [ ! -x "$VENV/bin/python" ]; then
  echo "creating venv at $VENV"
  python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

echo "installing google-colab-cli==0.7.5"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet "google-colab-cli==0.7.5"

# TASK-007.1: control-plane dependencies the controller needs in THIS
# interpreter (Drive service + token minting, the requests transport,
# and the YAML loader). The bare CLI venv lacked google-api-python-client
# and PyYAML, which made preflight fail on both interpreters (TASK-007
# acceptance). Pin PyYAML to the version the repo's CI and the system
# python already use; the rest follow the brief unpinned.
echo "installing control-plane dependencies"
"$PIP" install --quiet "google-api-python-client" "google-auth" "requests" "certifi" "pyyaml==6.0.1"

SITE="$("$PY" -c "import site; print(site.getsitepackages()[0])")"
echo "site-packages: $SITE"

# Patch 1: empty-POST 411 workaround (colab_cli/client.py).
if grep -q 'kwargs\["data"\] = b"{}"' "$SITE/colab_cli/client.py"; then
  echo "proxy_post_body patch already present, skipping"
else
  git -C "$SITE" apply --check "$PATCH_DIR/proxy_post_body.patch"
  git -C "$SITE" apply "$PATCH_DIR/proxy_post_body.patch"
  echo "applied proxy_post_body.patch"
fi

# Patch 2: drivemount timeout 120s -> 600s (colab_cli/commands/automation.py).
if grep -q 'timeout_ms=600000' "$SITE/colab_cli/commands/automation.py"; then
  echo "drivemount_timeout patch already present, skipping"
else
  git -C "$SITE" apply --check "$PATCH_DIR/drivemount_timeout.patch"
  git -C "$SITE" apply "$PATCH_DIR/drivemount_timeout.patch"
  echo "applied drivemount_timeout.patch"
fi

echo "verifying"
VERSION="$("$VENV/bin/colab" version)"
echo "  colab version: $VERSION"
case "$VERSION" in
  *0.7.5*) ;;
  *) echo "REFUSED: colab is not 0.7.5: $VERSION" >&2; exit 1 ;;
esac
"$PY" -c "
import sys; sys.path.insert(0, '$REPO')
from h3_controller import cli_post_body_patch_present, cli_drivemount_timeout_patch_present
roots = ['$SITE']
assert cli_post_body_patch_present(roots), 'POST-body patch marker missing after install'
assert cli_drivemount_timeout_patch_present(roots), 'drivemount-timeout patch marker missing after install'
print('  both patch markers present')
"
"$PY" -c "
import google.auth, googleapiclient, requests, certifi, yaml
print('  control-plane modules importable: google.auth, googleapiclient, requests, certifi, yaml')
"
echo "OK: $VENV/bin/colab is pinned 0.7.5 + both production patches"
echo "Use it via COLAB_CLI_PATH=$VENV/bin/colab, or install into the repo .venv"
