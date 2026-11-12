"""Build and publish the pinned ComfyUI source cache to Google Drive.

The cache lives OUTSIDE the model depot, in a dedicated folder
``Muse-H3-ComfyUI-Source`` in the same authorized Google account
(thewheeler1118@gmail.com). It holds exactly three files:

  comfyui-<commit>.tar.gz        git archive of the pinned commit
                                  (prefix comfyui/, plus a COMFYUI_COMMIT.txt
                                  marker carrying the commit id)
  comfyui-<commit>.tar.gz.sha256 hex SHA-256 of the archive
  comfyui-source-manifest.json   repo, commit, archive name, sha256, size,
                                  created_utc, source

The remote worker restores this archive instead of cloning from GitHub
whenever the cache is present, and verifies it (archive SHA-256 against
the task, then the embedded commit id). No model file and no READY
manifest is touched: the folder is separate from Muse-H3-Model-Depot.

Usage (from the project root):
  python3 h3_comfyui_cache.py build --repo comfyui-source --commit <sha>
      # local only: builds the archive + manifest under model_cache/
  python3 h3_comfyui_cache.py publish --repo comfyui-source --commit <sha> \
      --colab-auth adc --allow-drive-write
      # builds, creates the Drive folder, uploads the three files,
      # re-verifies each from server metadata, writes
      # model_cache/comfyui_source_manifest.json (0600, with Drive ids)
  python3 h3_comfyui_cache.py verify --colab-auth adc
      # read-only: re-checks the Drive files against the local manifest
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from drive_api import file_digests, verify_uploaded_file
from drive_depot import (
    account_matches,
    atomic_json,
    drive_service,
    ensure_folder,
    upload_one,
)

COMFYUI_REPO_URL = "https://github.com/Comfy-Org/ComfyUI.git"
CACHE_FOLDER_NAME = "Muse-H3-ComfyUI-Source"
CACHE_DRIVE_DIR = "/content/drive/MyDrive/Muse-H3-ComfyUI-Source"
LOCAL_MANIFEST = ROOT / "model_cache" / "comfyui_source_manifest.json"
STAGING = ROOT / "model_cache" / "comfyui_source"


def archive_name(commit: str) -> str:
    return f"comfyui-{commit}.tar.gz"


def build_archive(repo_dir: Path, commit: str, out_path: Path) -> str:
    """git archive of <commit> (+ COMFYUI_COMMIT.txt marker), gzipped
    deterministically (mtime=0). Returns the SHA-256 of the .tar.gz."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "archive", commit, "--prefix=comfyui/"],
        capture_output=True, check=True, timeout=600)
    raw_tar = proc.stdout
    if not raw_tar:
        raise RuntimeError("git archive produced no output for " + commit)
    with tempfile.TemporaryDirectory() as d:
        tar_path = Path(d) / "comfyui.tar"
        tar_path.write_bytes(raw_tar)
        with tarfile.open(tar_path, "a") as tf:
            data = (commit + "\n").encode()
            info = tarfile.TarInfo("comfyui/COMFYUI_COMMIT.txt")
            info.size = len(data)
            info.mtime = 0
            tf.addfile(info, io.BytesIO(data))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tar_path.open("rb") as fi, gzip.GzipFile(
                filename="", mode="wb", fileobj=out_path.open("wb"),
                compresslevel=9, mtime=0) as gz:
            for chunk in iter(lambda: fi.read(4 * 1024 * 1024), b""):
                gz.write(chunk)
    return file_digests(out_path)["sha256"]


def build_manifest(repo_url: str, commit: str, name: str, sha256: str,
                   size: int) -> dict:
    return {
        "repo": repo_url,
        "commit": commit,
        "archive_name": name,
        "sha256": sha256,
        "size_bytes": size,
        "created_utc": datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "git archive of the pinned ComfyUI commit, "
                  "plus COMFYUI_COMMIT.txt marker",
        "cache_folder": CACHE_FOLDER_NAME,
        "cache_drive_dir": CACHE_DRIVE_DIR,
    }


def verify_local_archive(repo_dir: Path, commit: str, archive_path: Path,
                         expected_sha256: str) -> dict:
    """Independent local check: SHA-256, extract, marker commit, and the
    extracted tree's files match the pinned commit's tree."""
    digests = file_digests(archive_path)
    if digests["sha256"] != expected_sha256:
        raise RuntimeError("local archive SHA-256 mismatch")
    with tempfile.TemporaryDirectory() as d:
        with tarfile.open(archive_path, "r:gz") as tf:
            tf.extractall(d, filter="data")
        marker = Path(d) / "comfyui" / "COMFYUI_COMMIT.txt"
        if not marker.is_file() or marker.read_text().strip() != commit:
            raise RuntimeError("local archive commit marker mismatch")
        for name in ("main.py", "requirements.txt"):
            if not (Path(d) / "comfyui" / name).is_file():
                raise RuntimeError("local archive missing " + name)
        # Tree identity: the extracted tree must equal the pinned commit's.
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", commit + "^{tree}"],
            capture_output=True, text=True, check=True, timeout=60)
        tree = proc.stdout.strip()
        listed = subprocess.run(
            ["git", "-C", str(repo_dir), "ls-tree", "-r", "--format=%(objectname) %(path)",
             commit],
            capture_output=True, text=True, check=True, timeout=60)
        expected = {}
        for line in listed.stdout.splitlines():
            oid, path = line.split(" ", 1)
            expected[path] = oid
        import hashlib as _h
        for path, oid in expected.items():
            data = (Path(d) / "comfyui" / path).read_bytes()
            blob = _h.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if blob != oid:
                raise RuntimeError("extracted file differs from pinned tree: " + path)
    return {"archive_sha256": digests["sha256"], "size_bytes": digests["size"],
            "tree": tree, "files_checked": len(expected)}


def publish(repo_dir: Path, commit: str, colab_auth: str) -> dict:
    """Build the archive locally, create the Drive folder, upload the three
    files, re-verify each from server metadata, and write the local
    manifest with Drive ids. Never touches the model depot or READY."""
    if not (repo_dir / ".git").exists():
        raise RuntimeError("repo dir has no .git: " + str(repo_dir))
    head = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True,
                          timeout=60).stdout.strip()
    if head != commit:
        raise RuntimeError("repo HEAD " + head + " != pinned commit " + commit)
    service = drive_service()
    if not account_matches(service, colab_auth):
        raise RuntimeError("ACCOUNT_MISMATCH")
    STAGING.mkdir(parents=True, exist_ok=True)
    name = archive_name(commit)
    archive_path = STAGING / name
    sha256 = build_archive(repo_dir, commit, archive_path)
    digests = file_digests(archive_path)
    verify_local_archive(repo_dir, commit, archive_path, sha256)
    sha_path = STAGING / (name + ".sha256")
    sha_path.write_text(sha256 + "  " + name + "\n")
    manifest = build_manifest(COMFYUI_REPO_URL, commit, name, sha256,
                              digests["size"])
    manifest_path = STAGING / "comfyui-source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    folder_id = ensure_folder(service, None, CACHE_FOLDER_NAME,
                              allow_write=True)
    entries = {}
    for local_path, rel in ((archive_path, name),
                            (sha_path, name + ".sha256"),
                            (manifest_path, "comfyui-source-manifest.json")):
        entry = upload_one(service, local_path, folder_id, rel, rel,
                           file_digests(local_path)["sha256"], commit)
        entries[rel] = entry
    # Independent re-verification from server metadata for every file.
    for rel, entry in entries.items():
        verify_uploaded_file(service, entry["drive_file_id"],
                             STAGING / rel)
    manifest["drive_folder_id"] = folder_id
    manifest["drive_files"] = {
        rel: {"id": e["drive_file_id"], "size": e["size"],
              "sha256": e["sha256"], "server_md5": e["server_md5"]}
        for rel, e in entries.items()
    }
    atomic_json(LOCAL_MANIFEST, manifest)
    os.chmod(LOCAL_MANIFEST, 0o600)
    return manifest


def verify(colab_auth: str) -> dict:
    """Read-only: re-check the Drive cache files against the local manifest."""
    if not LOCAL_MANIFEST.exists():
        raise RuntimeError("local manifest missing: " + str(LOCAL_MANIFEST))
    manifest = json.loads(LOCAL_MANIFEST.read_text())
    service = drive_service()
    if not account_matches(service, colab_auth):
        raise RuntimeError("ACCOUNT_MISMATCH")
    result = {"folder_id": manifest.get("drive_folder_id"), "files": {}}
    for rel, info in manifest.get("drive_files", {}).items():
        proof = verify_uploaded_file(
            service, info["id"], STAGING / rel)
        result["files"][rel] = {
            "drive_file_id": info["id"], "size": proof["size"],
            "sha256": proof["sha256"], "server_verified": True}
    return result


def cli() -> int:
    p = argparse.ArgumentParser(description="Pinned ComfyUI source cache")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--repo", required=True)
    b.add_argument("--commit", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("--repo", required=True)
    pub.add_argument("--commit", required=True)
    pub.add_argument("--colab-auth", required=True)
    pub.add_argument("--allow-drive-write", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--colab-auth", required=True)
    args = p.parse_args()
    if args.cmd == "build":
        repo = Path(args.repo)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True,
                              timeout=60).stdout.strip()
        if head != args.commit:
            raise RuntimeError("repo HEAD " + head + " != pinned commit " + args.commit)
        STAGING.mkdir(parents=True, exist_ok=True)
        name = archive_name(args.commit)
        archive_path = STAGING / name
        sha256 = build_archive(repo, args.commit, archive_path)
        digests = file_digests(archive_path)
        checked = verify_local_archive(repo, args.commit, archive_path, sha256)
        (STAGING / (name + ".sha256")).write_text(sha256 + "  " + name + "\n")
        manifest = build_manifest(COMFYUI_REPO_URL, args.commit, name,
                                  sha256, digests["size"])
        (STAGING / "comfyui-source-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n")
        print(json.dumps({"archive": name, "sha256": sha256,
                          "size_bytes": digests["size"],
                          "tree": checked["tree"],
                          "files_checked": checked["files_checked"]},
                         indent=2))
        return 0
    if args.cmd == "publish":
        if not args.allow_drive_write:
            p.error("Mutating Drive requires --allow-drive-write")
        manifest = publish(Path(args.repo), args.commit, args.colab_auth)
        print(json.dumps({"folder": manifest["cache_folder"],
                          "drive_folder_id": manifest["drive_folder_id"],
                          "archive": manifest["archive_name"],
                          "sha256": manifest["sha256"],
                          "size_bytes": manifest["size_bytes"],
                          "local_manifest": str(LOCAL_MANIFEST)},
                         indent=2))
        return 0
    if args.cmd == "verify":
        result = verify(args.colab_auth)
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(cli())
