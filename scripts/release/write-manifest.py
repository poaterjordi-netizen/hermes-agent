#!/usr/bin/env python3
"""Write manifest.json for a Hermes release bundle.

Phase 0 task 0.4: Every bundle carries integrity + compat metadata.

manifest.json schema:
    {
      "schema": 1,
      "version": "2026.07.14",
      "channel": "nightly",
      "git_sha": "<40-hex>",
      "platform": "linux-x64",
      "min_updater_version": "0.1.0",
      "desktop": true,
      "files": { "runtime/venv/bin/python": "sha256:...", "...": "..." }
    }

The signature is a minisign signature over manifest.json itself, shipped as
manifest.json.minisig — verify-manifest-then-verify-files gives whole-bundle
integrity with one signature.

Usage:
    python scripts/release/write-manifest.py --bundle-dir dist/bundle \
        --version 2026.07.14 --channel nightly --platform linux-x64 \
        --git-sha $(git rev-parse HEAD) [--minisign-key /path/to/seckey]

    # Verify a bundle:
    python scripts/release/write-manifest.py --verify --bundle-dir dist/bundle \
        --pubkey /path/to/pubkey
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA = 1
DEFAULT_MIN_UPDATER_VERSION = "0.1.0"
CHUNK_SIZE = 65536


def compute_file_hash(path: Path) -> str:
    """Compute sha256 hash of a file, returning 'sha256:<hex>'."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def collect_file_hashes(bundle_dir: Path) -> dict[str, str]:
    """Walk every regular file in the bundle, computing sha256 hashes.

    Returns a dict of relative_path -> 'sha256:<hex>'.
    Skips manifest.json and manifest.json.minisig (they're the output, not input).
    """
    files: dict[str, str] = {}
    for root, dirs, filenames in os.walk(bundle_dir):
        # Skip .staging dirs
        dirs[:] = [d for d in dirs if d != ".staging"]
        for filename in filenames:
            filepath = Path(root) / filename
            if not filepath.is_file():
                continue
            rel = filepath.relative_to(bundle_dir)
            rel_str = str(rel)
            # Skip manifest files — they're written after hashing
            if rel_str in ("manifest.json", "manifest.json.minisig"):
                continue
            files[rel_str] = compute_file_hash(filepath)
    return files


def write_manifest(
    bundle_dir: Path,
    *,
    version: str,
    channel: str,
    git_sha: str,
    platform: str,
    min_updater_version: str = DEFAULT_MIN_UPDATER_VERSION,
    desktop: bool = False,
    extra_fields: dict | None = None,
) -> dict:
    """Write manifest.json for a bundle directory.

    Returns the manifest dict.
    """
    manifest: dict = {
        "schema": SCHEMA,
        "version": version,
        "channel": channel,
        "git_sha": git_sha,
        "platform": platform,
        "min_updater_version": min_updater_version,
        "desktop": desktop,
    }
    if extra_fields:
        manifest.update(extra_fields)
    manifest["files"] = collect_file_hashes(bundle_dir)

    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify_file_hashes(bundle_dir: Path, manifest: dict) -> tuple[bool, list[str]]:
    """Verify every file hash in the manifest matches the actual files.

    Returns (ok, errors).
    """
    errors: list[str] = []
    files = manifest.get("files", {})

    # Check every file in the manifest exists and matches
    for rel_path, expected_hash in files.items():
        filepath = bundle_dir / rel_path
        if not filepath.exists():
            errors.append(f"missing: {rel_path}")
            continue
        actual_hash = compute_file_hash(filepath)
        if actual_hash != expected_hash:
            errors.append(f"tampered: {rel_path} (expected {expected_hash}, got {actual_hash})")

    # Check for extra files not in the manifest
    actual_files = set()
    for root, dirs, filenames in os.walk(bundle_dir):
        dirs[:] = [d for d in dirs if d != ".staging"]
        for filename in filenames:
            filepath = Path(root) / filename
            rel = str(filepath.relative_to(bundle_dir))
            if rel in ("manifest.json", "manifest.json.minisig"):
                continue
            actual_files.add(rel)

    manifest_files = set(files.keys())
    extra = actual_files - manifest_files
    for rel in sorted(extra):
        errors.append(f"extra file not in manifest: {rel}")

    return (len(errors) == 0, errors)


def sign_manifest(bundle_dir: Path, seckey_path: Path | None = None) -> bool:
    """Sign manifest.json with minisign, producing manifest.json.minisig.

    Returns True on success, False if minisign is not available.
    Raises on signing failure.
    """
    manifest_path = bundle_dir / "manifest.json"
    sig_path = bundle_dir / "manifest.json.minisig"

    minisign = _find_minisign()
    if minisign is None:
        print("WARN: minisign not found — skipping signature", file=sys.stderr)
        return False

    cmd = [minisign, "-S", "-x", str(sig_path), "-m", str(manifest_path)]
    if seckey_path:
        cmd.extend(["-s", str(seckey_path)])
    # -W: no password (for CI / automated signing with throwaway keys)
    cmd.append("-W")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"minisign signing failed: {result.stderr}")
    return True


def verify_signature(bundle_dir: Path, pubkey_path: Path | None = None) -> bool:
    """Verify the minisign signature on manifest.json.

    Returns True if signature is valid, False otherwise.
    """
    manifest_path = bundle_dir / "manifest.json"
    sig_path = bundle_dir / "manifest.json.minisig"

    if not sig_path.exists():
        return False

    minisign = _find_minisign()
    if minisign is None:
        print("WARN: minisign not found — cannot verify signature", file=sys.stderr)
        return False

    cmd = [minisign, "-V", "-x", str(sig_path), "-m", str(manifest_path), "-q"]
    if pubkey_path:
        cmd.extend(["-p", str(pubkey_path)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def _find_minisign() -> str | None:
    """Find minisign executable."""
    import shutil
    return shutil.which("minisign")


def main():
    parser = argparse.ArgumentParser(description="Write or verify bundle manifest")
    parser.add_argument("--bundle-dir", required=True, help="Bundle directory")
    parser.add_argument("--version", help="Bundle version (e.g. 2026.07.14)")
    parser.add_argument("--channel", default="nightly", help="Release channel")
    parser.add_argument("--git-sha", help="Git commit SHA")
    parser.add_argument("--platform", help="Target platform (e.g. linux-x64)")
    parser.add_argument("--min-updater-version", default=DEFAULT_MIN_UPDATER_VERSION)
    parser.add_argument("--desktop", action="store_true", help="Bundle includes desktop app")
    parser.add_argument("--minisign-key", help="Path to minisign secret key")
    parser.add_argument("--verify", action="store_true", help="Verify existing manifest")
    parser.add_argument("--pubkey", help="Path to minisign public key for verification")
    args = parser.parse_args()

    bundle_dir = Path(args.bundle_dir).resolve()

    if args.verify:
        manifest_path = bundle_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"ERROR: {manifest_path} not found", file=sys.stderr)
            sys.exit(1)
        manifest = json.loads(manifest_path.read_text())
        ok, errors = verify_file_hashes(bundle_dir, manifest)
        if ok:
            print("PASS: all file hashes verified")
        else:
            print("FAIL: hash verification errors:")
            for e in errors:
                print(f"  {e}")
            sys.exit(1)
        if args.pubkey:
            if verify_signature(bundle_dir, Path(args.pubkey)):
                print("PASS: signature verified")
            else:
                print("FAIL: signature verification failed", file=sys.stderr)
                sys.exit(1)
        return

    # Write mode
    if not all([args.version, args.git_sha, args.platform]):
        print("ERROR: --version, --git-sha, and --platform are required for writing", file=sys.stderr)
        sys.exit(1)

    manifest = write_manifest(
        bundle_dir,
        version=args.version,
        channel=args.channel,
        git_sha=args.git_sha,
        platform=args.platform,
        min_updater_version=args.min_updater_version,
        desktop=args.desktop,
    )
    print(f"Wrote manifest.json with {len(manifest['files'])} file hashes")

    seckey = Path(args.minisign_key) if args.minisign_key else None
    if sign_manifest(bundle_dir, seckey):
        print("Signed manifest.json.minisig")


if __name__ == "__main__":
    main()
