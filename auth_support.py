"""Muse-local executable and credential *metadata* guards (never reads OAuth secrets)."""
from __future__ import annotations
import os, shutil, stat, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def resolve_colab_executable() -> str:
    """Prefer the active project's venv, then the explicit override and PATH.

    Never guess a macOS installation path; supports `python script.py` and
    `.venv/bin/python script.py` launching in a minimal Muse shell.
    """
    override = os.environ.get('COLAB_CLI_PATH')
    candidates = ([Path(override).expanduser()] if override else []) + [
        Path(sys.executable).parent / 'colab',
        ROOT / '.venv' / 'bin' / 'colab',
    ]
    path = shutil.which('colab')
    if path:
        candidates.append(Path(path))
    candidates.append(Path.home() / '.local/bin/colab')
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise FileNotFoundError('Colab CLI missing; activate project venv or install official google-colab-cli')


def audit_user_adc_file() -> dict:
    """Metadata-only: no token JSON, stdout of gcloud, or secret is read."""
    p = Path.home() / '.config/gcloud/application_default_credentials.json'
    if not p.exists():
        return {'present': False, 'path': str(p), 'credentials_read': False}
    if p.is_symlink() or not p.is_file():
        raise RuntimeError('ADC path is symlink/not regular file')
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise RuntimeError('ADC credential permissions are not private (expected mode 0600)')
    return {'present': True, 'mode': oct(mode), 'credentials_read': False}
