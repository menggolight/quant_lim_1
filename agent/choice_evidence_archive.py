"""Copy a local Choice diagnostic evidence tree into an append-only archive.

Both roots are explicit.  The source is inventoried without modification and
every copied regular file is bound to its SHA-256.  Credential-shaped paths are
excluded before their contents are opened.  Symlinks/reparse points, special
files, path overlap and writes outside the repository fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.providers.base import safe_error_text


ARCHIVE_VERSION = "choice-evidence-archive-v1"
MANIFEST_VERSION = "choice-evidence-archive-manifest-v1"
CHECKPOINT_VERSION = "choice-evidence-archive-checkpoint-v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_COMPONENT = re.compile(
    r"(?:activation|loginactivator|user[_-]?info|credential|token|secret|"
    r"password|passwd|cookie|authorization|api[_-]?key|access[_-]?key|"
    r"account[_-]?(?:id|no|number)|shareholder[_-]?account|otp)",
    re.IGNORECASE,
)
_SENSITIVE_SUFFIXES = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"}
)


class ChoiceEvidenceArchiveError(RuntimeError):
    """Fail-closed archive boundary error."""


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "object_path": f"objects/{self.sha256[:2]}/{self.sha256}.blob",
        }


@dataclass(frozen=True)
class Inventory:
    source_root_relative: str
    entries: tuple[InventoryEntry, ...]
    excluded_sensitive_categories: tuple[str, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    @property
    def tree_sha256(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "source_root_relative": self.source_root_relative,
                    "files": [entry.to_dict() for entry in self.entries],
                    "excluded_sensitive_path_count": len(
                        self.excluded_sensitive_categories
                    ),
                    "excluded_sensitive_categories": sorted(
                        set(self.excluded_sensitive_categories)
                    ),
                }
            )
        )


def _is_sensitive_component(component: str) -> bool:
    lowered = component.casefold()
    return (
        lowered == ".env"
        or lowered.startswith(".env.")
        or Path(component).suffix.casefold() in _SENSITIVE_SUFFIXES
        or _SENSITIVE_COMPONENT.search(component) is not None
    )


def _sensitive_category(component: str) -> str:
    lowered = component.casefold()
    if lowered == ".env" or lowered.startswith(".env."):
        return "dotenv"
    if Path(component).suffix.casefold() in _SENSITIVE_SUFFIXES:
        return "key_material"
    for category, marker in (
        ("activation", "activation"),
        ("user_info", "userinfo"),
        ("user_info", "user_info"),
        ("user_info", "user-info"),
        ("credential", "credential"),
        ("token", "token"),
        ("secret", "secret"),
        ("password", "password"),
        ("password", "passwd"),
        ("cookie", "cookie"),
        ("authorization", "authorization"),
        ("api_key", "api_key"),
        ("api_key", "api-key"),
        ("access_key", "access_key"),
        ("access_key", "access-key"),
        ("account_identifier", "account_id"),
        ("account_identifier", "account-id"),
        ("account_identifier", "account_no"),
        ("account_identifier", "account-no"),
        ("otp", "otp"),
    ):
        if marker in lowered:
            return category
    return "credential_shaped"


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _reject_link_or_reparse(path: Path) -> os.stat_result:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ChoiceEvidenceArchiveError("unable to inspect archive source path") from exc
    if stat.S_ISLNK(details.st_mode) or _is_reparse(details):
        raise ChoiceEvidenceArchiveError(
            "archive source cannot contain symlinks or reparse points"
        )
    return details


def _resolve_inside_repository(
    path: Path | str,
    *,
    repository_root: Path,
    label: str,
) -> tuple[Path, Path]:
    root = repository_root.resolve()
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ChoiceEvidenceArchiveError(
            f"{label} must remain inside the repository"
        ) from exc
    if not relative.parts:
        raise ChoiceEvidenceArchiveError(f"{label} cannot be the repository root")
    return resolved, relative


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _hash_regular_file(path: Path) -> tuple[int, str]:
    """Hash a stable regular file, rejecting mutation during the read."""

    before = _reject_link_or_reparse(path)
    if not stat.S_ISREG(before.st_mode):
        raise ChoiceEvidenceArchiveError("archive source contains a special file")
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ChoiceEvidenceArchiveError("unable to read archive source file") from exc
    after = _reject_link_or_reparse(path)
    stable_fields = (
        before.st_size,
        before.st_mtime_ns,
        getattr(before, "st_ino", 0),
    )
    after_fields = (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_ino", 0),
    )
    if stable_fields != after_fields:
        raise ChoiceEvidenceArchiveError("archive source changed during inventory")
    return before.st_size, digest.hexdigest()


def _walk_source(source_root: Path) -> Iterable[tuple[Path, tuple[str, ...]]]:
    """Yield safe files; report excluded paths without opening their contents."""

    for current_text, directory_names, file_names in os.walk(
        source_root, topdown=True, followlinks=False
    ):
        current = Path(current_text)
        _reject_link_or_reparse(current)
        directory_names.sort()
        file_names.sort()
        safe_directories: list[str] = []
        excluded: list[str] = []
        for name in directory_names:
            candidate = current / name
            if _is_sensitive_component(name):
                excluded.append(_sensitive_category(name))
                continue
            _reject_link_or_reparse(candidate)
            safe_directories.append(name)
        directory_names[:] = safe_directories
        for value in excluded:
            yield Path(), (value,)
        for name in file_names:
            candidate = current / name
            if _is_sensitive_component(name):
                yield Path(), (_sensitive_category(name),)
                continue
            yield candidate, ()


def build_inventory(
    source_root: Path | str,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> Inventory:
    """Build a deterministic read-only inventory of safe regular files."""

    repository = Path(repository_root).resolve()
    source, source_relative = _resolve_inside_repository(
        source_root, repository_root=repository, label="source_root"
    )
    if any(_is_sensitive_component(part) for part in source_relative.parts):
        raise ChoiceEvidenceArchiveError(
            "source_root itself is credential-shaped and cannot be inventoried"
        )
    if not source.is_dir():
        raise ChoiceEvidenceArchiveError("source_root must be an existing directory")
    _reject_link_or_reparse(source)
    entries: list[InventoryEntry] = []
    excluded: list[str] = []
    for path, exclusions in _walk_source(source):
        if exclusions:
            excluded.extend(exclusions)
            continue
        relative = path.relative_to(source).as_posix()
        size, digest = _hash_regular_file(path)
        entries.append(
            InventoryEntry(relative_path=relative, size=size, sha256=digest)
        )
    entries.sort(key=lambda item: item.relative_path)
    excluded.sort()
    if not entries:
        raise ChoiceEvidenceArchiveError(
            "source_root has no safe regular evidence files to archive"
        )
    return Inventory(
        source_root_relative=source_relative.as_posix(),
        entries=tuple(entries),
        excluded_sensitive_categories=tuple(excluded),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise ChoiceEvidenceArchiveError(
                "refusing non-identical content-addressed archive collision"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=".choice-archive-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not path.is_file() or path.read_bytes() != content:
                raise ChoiceEvidenceArchiveError(
                    "refusing non-identical content-addressed archive collision"
                )
    finally:
        temporary.unlink(missing_ok=True)


def _copy_verified_object(
    source: Path,
    destination: Path,
    expected: InventoryEntry,
) -> None:
    if destination.exists():
        size, digest = _hash_regular_file(destination)
        if size != expected.size or digest != expected.sha256:
            raise ChoiceEvidenceArchiveError(
                "existing content-addressed archive object is corrupted"
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = _reject_link_or_reparse(source)
    if not stat.S_ISREG(before.st_mode):
        raise ChoiceEvidenceArchiveError("archive source contains a special file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".choice-object-", dir=destination.parent)
    temporary = Path(temporary_name)
    digest = sha256()
    copied = 0
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(block)
                digest.update(block)
                copied += len(block)
            writer.flush()
            os.fsync(writer.fileno())
        after = _reject_link_or_reparse(source)
        before_fields = (
            before.st_size,
            before.st_mtime_ns,
            getattr(before, "st_ino", 0),
        )
        after_fields = (
            after.st_size,
            after.st_mtime_ns,
            getattr(after, "st_ino", 0),
        )
        if before_fields != after_fields:
            raise ChoiceEvidenceArchiveError("archive source changed during copy")
        if copied != expected.size or digest.hexdigest() != expected.sha256:
            raise ChoiceEvidenceArchiveError(
                "archive source differs from the completed inventory"
            )
        try:
            os.link(temporary, destination)
        except FileExistsError:
            size, existing_digest = _hash_regular_file(destination)
            if size != expected.size or existing_digest != expected.sha256:
                raise ChoiceEvidenceArchiveError(
                    "existing content-addressed archive object is corrupted"
                )
    finally:
        temporary.unlink(missing_ok=True)


def archive_choice_evidence(
    source_root: Path | str,
    output_root: Path | str,
    *,
    repository_root: Path | str = REPOSITORY_ROOT,
) -> dict[str, Any]:
    """Inventory and copy a safe Choice evidence tree; never alter the source."""

    repository = Path(repository_root).resolve()
    source, _ = _resolve_inside_repository(
        source_root, repository_root=repository, label="source_root"
    )
    output, _ = _resolve_inside_repository(
        output_root, repository_root=repository, label="output_root"
    )
    if _paths_overlap(source, output):
        raise ChoiceEvidenceArchiveError(
            "source_root and output_root must not contain one another"
        )
    inventory = build_inventory(source, repository_root=repository)
    for entry in inventory.entries:
        source_file = source / Path(entry.relative_path)
        destination = output / "objects" / entry.sha256[:2] / f"{entry.sha256}.blob"
        _copy_verified_object(source_file, destination, entry)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "archive_version": ARCHIVE_VERSION,
        "source_root_relative": inventory.source_root_relative,
        "source_tree_sha256": inventory.tree_sha256,
        "file_count": len(inventory.entries),
        "total_bytes": inventory.total_bytes,
        "files": [entry.to_dict() for entry in inventory.entries],
        "excluded_sensitive_path_count": len(inventory.excluded_sensitive_categories),
        "excluded_sensitive_categories": sorted(
            set(inventory.excluded_sensitive_categories)
        ),
        "source_deleted": False,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_sha256 = sha256_bytes(manifest_bytes)
    manifest_path = output / "manifests" / manifest_sha256[:2] / f"{manifest_sha256}.json"
    _atomic_write(manifest_path, manifest_bytes)
    checkpoint = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "archive_version": ARCHIVE_VERSION,
        "source_tree_sha256": inventory.tree_sha256,
        "archive_manifest_sha256": manifest_sha256,
        "archive_manifest_path": manifest_path.relative_to(output).as_posix(),
        "file_count": len(inventory.entries),
        "total_bytes": inventory.total_bytes,
        "status": "completed",
        "source_deleted": False,
    }
    checkpoint_bytes = canonical_json_bytes(checkpoint)
    checkpoint_sha256 = sha256_bytes(checkpoint_bytes)
    checkpoint_path = (
        output
        / "checkpoints"
        / inventory.tree_sha256[:2]
        / inventory.tree_sha256
        / f"{checkpoint_sha256}.json"
    )
    _atomic_write(checkpoint_path, checkpoint_bytes)
    return {
        "status": "passed",
        "archive_version": ARCHIVE_VERSION,
        "source_root_relative": inventory.source_root_relative,
        "source_tree_sha256": inventory.tree_sha256,
        "file_count": len(inventory.entries),
        "total_bytes": inventory.total_bytes,
        "excluded_sensitive_path_count": len(inventory.excluded_sensitive_categories),
        "excluded_sensitive_categories": sorted(
            set(inventory.excluded_sensitive_categories)
        ),
        "manifest_sha256": manifest_sha256,
        "manifest_path": manifest_path.relative_to(output).as_posix(),
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_path": checkpoint_path.relative_to(output).as_posix(),
        "source_deleted": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = archive_choice_evidence(args.source_root, args.output_root)
    except Exception as exc:
        result = {
            "status": "failed",
            "archive_version": ARCHIVE_VERSION,
            "source_deleted": False,
            "error_type": type(exc).__name__,
            "error": safe_error_text(exc),
        }
    sys.stdout.write(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
