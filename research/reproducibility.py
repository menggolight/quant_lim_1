"""Repository-state evidence shared by formal research artifacts."""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path


def _sha256_bytes(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def git_worktree_state(
    workspace: Path,
) -> tuple[str | None, bool | None, str | None]:
    """Return commit, dirty state and a content-bound dirty-tree hash.

    A dirty hash includes both the binary tracked diff and every untracked
    file's relative path/content hash.  A second snapshot detects concurrent
    changes instead of sealing ambiguous evidence.
    """

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None, None
    if not commit:
        return None, None, None
    dirty = bool(status.strip())
    if not dirty:
        try:
            commit_after = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            status_after = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z"],
                cwd=workspace,
                check=True,
                capture_output=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return None, None, None
        if commit_after != commit or status_after != status:
            return None, None, None
        return commit, False, None
    try:
        tracked_diff = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
        untracked_raw = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
        untracked: list[tuple[str, str]] = []
        for encoded_path in untracked_raw.split(b"\0"):
            if not encoded_path:
                continue
            relative = encoded_path.decode("utf-8", errors="surrogateescape")
            source_path = workspace / Path(relative)
            if not source_path.is_file():
                raise OSError(f"untracked file changed during hashing: {relative}")
            untracked.append(
                (relative.replace("\\", "/"), _sha256_bytes(source_path.read_bytes()))
            )
        status_after = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
        tracked_diff_after = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
        untracked_raw_after = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=workspace,
            check=True,
            capture_output=True,
        ).stdout
        commit_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if (
            commit_after != commit
            or status_after != status
            or tracked_diff_after != tracked_diff
            or untracked_raw_after != untracked_raw
        ):
            return None, None, None
        for relative, digest in untracked:
            source_path = workspace / Path(relative)
            if not source_path.is_file() or _sha256_bytes(source_path.read_bytes()) != digest:
                return commit or None, True, None
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return commit, True, None
    payload = {
        "tracked_diff_sha256": _sha256_bytes(tracked_diff),
        "untracked_files": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(untracked)
        ],
    }
    return commit, True, _sha256_bytes(_canonical_json_bytes(payload))


__all__ = ["git_worktree_state"]
