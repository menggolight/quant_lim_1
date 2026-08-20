from __future__ import annotations

import json
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from agent.choice_evidence_archive import (
    ChoiceEvidenceArchiveError,
    archive_choice_evidence,
    build_inventory,
    build_parser,
)


def file_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class ChoiceEvidenceArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Path(self.temp.name) / "repo"
        self.repository.mkdir()
        self.source = self.repository / ".tmp" / "choice_diag_cache"
        self.output = self.repository / "data" / "evidence_archive" / "choice"
        fixtures = {
            "raw/aa/source.raw": b"raw-response",
            "receipt/receipt.json": b'{"status":"diagnostic"}\n',
            "quarantine/rejected.raw": b"rejected-response",
            "checkpoint/checkpoint.json": b'{"status":"complete"}\n',
        }
        for relative, content in fixtures.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_archive_copies_all_safe_evidence_layers_and_preserves_source(self) -> None:
        before = file_snapshot(self.source)
        result = archive_choice_evidence(
            self.source, self.output, repository_root=self.repository
        )
        after = file_snapshot(self.source)
        self.assertEqual(before, after)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["source_deleted"])
        self.assertEqual(result["file_count"], 4)
        manifest_path = self.output / str(result["manifest_path"])
        checkpoint_path = self.output / str(result["checkpoint_path"])
        self.assertTrue(manifest_path.is_file())
        self.assertTrue(checkpoint_path.is_file())
        self.assertEqual(sha256(manifest_path.read_bytes()).hexdigest(), result["manifest_sha256"])
        self.assertEqual(
            sha256(checkpoint_path.read_bytes()).hexdigest(),
            result["checkpoint_sha256"],
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        paths = {item["relative_path"] for item in manifest["files"]}
        self.assertEqual(paths, set(before))
        for item in manifest["files"]:
            archive_object = self.output / item["object_path"]
            self.assertTrue(archive_object.is_file())
            self.assertEqual(sha256(archive_object.read_bytes()).hexdigest(), item["sha256"])

    def test_archive_excludes_sensitive_paths_without_copying_them(self) -> None:
        sensitive_files = {
            "activation/LoginActivator.exe": b"binary-secret-loader",
            "userInfo/profile.json": b'{"account":"not-for-archive"}',
            "credential.txt": b"do-not-read-or-copy",
            ".env": b"CHOICE_TOKEN=do-not-read-or-copy",
        }
        for relative, content in sensitive_files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        result = archive_choice_evidence(
            self.source, self.output, repository_root=self.repository
        )
        manifest = json.loads(
            (self.output / str(result["manifest_path"])).read_text(encoding="utf-8")
        )
        included = {item["relative_path"] for item in manifest["files"]}
        self.assertTrue(set(included).isdisjoint(sensitive_files))
        self.assertEqual(manifest["excluded_sensitive_path_count"], 4)
        excluded = set(manifest["excluded_sensitive_categories"])
        self.assertIn("activation", excluded)
        self.assertIn("user_info", excluded)
        self.assertIn("credential", excluded)
        self.assertIn("dotenv", excluded)
        rendered_manifest = json.dumps(manifest, ensure_ascii=False)
        for relative in sensitive_files:
            self.assertNotIn(relative, rendered_manifest)
        archived_bytes = b"".join(
            path.read_bytes() for path in (self.output / "objects").rglob("*.blob")
        )
        self.assertNotIn(b"do-not-read-or-copy", archived_bytes)

    def test_inventory_is_deterministic_and_hashes_file_bytes(self) -> None:
        first = build_inventory(self.source, repository_root=self.repository)
        second = build_inventory(self.source, repository_root=self.repository)
        self.assertEqual(first, second)
        self.assertEqual(first.tree_sha256, second.tree_sha256)
        by_path = {entry.relative_path: entry for entry in first.entries}
        expected = sha256((self.source / "raw/aa/source.raw").read_bytes()).hexdigest()
        self.assertEqual(by_path["raw/aa/source.raw"].sha256, expected)

    def test_identical_rerun_is_idempotent(self) -> None:
        first = archive_choice_evidence(
            self.source, self.output, repository_root=self.repository
        )
        second = archive_choice_evidence(
            self.source, self.output, repository_root=self.repository
        )
        self.assertEqual(first, second)
        self.assertEqual(
            len(list((self.output / "manifests").rglob("*.json"))), 1
        )

    def test_corrupted_existing_object_is_rejected(self) -> None:
        result = archive_choice_evidence(
            self.source, self.output, repository_root=self.repository
        )
        manifest = json.loads(
            (self.output / str(result["manifest_path"])).read_text(encoding="utf-8")
        )
        target = self.output / manifest["files"][0]["object_path"]
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(ChoiceEvidenceArchiveError, "corrupted"):
            archive_choice_evidence(
                self.source, self.output, repository_root=self.repository
            )

    def test_source_and_output_must_not_overlap(self) -> None:
        with self.assertRaisesRegex(ChoiceEvidenceArchiveError, "must not contain"):
            archive_choice_evidence(
                self.source,
                self.source / "persistent",
                repository_root=self.repository,
            )
        self.assertFalse((self.source / "persistent").exists())

    def test_source_and_output_must_remain_inside_repository(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ChoiceEvidenceArchiveError, "inside the repository"):
            archive_choice_evidence(
                outside,
                self.output,
                repository_root=self.repository,
            )
        with self.assertRaisesRegex(ChoiceEvidenceArchiveError, "inside the repository"):
            archive_choice_evidence(
                self.source,
                outside / "archive",
                repository_root=self.repository,
            )

    def test_symlink_or_reparse_source_entry_is_rejected(self) -> None:
        target = self.source / "raw" / "aa" / "source.raw"
        link = self.source / "linked.raw"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this Windows runtime")
        with self.assertRaisesRegex(ChoiceEvidenceArchiveError, "symlinks or reparse"):
            build_inventory(self.source, repository_root=self.repository)

    def test_all_sensitive_source_files_fail_without_creating_output(self) -> None:
        sensitive_root = self.repository / ".tmp" / "only_sensitive"
        sensitive_root.mkdir(parents=True)
        (sensitive_root / "access_token.txt").write_bytes(b"not-opened")
        with self.assertRaisesRegex(ChoiceEvidenceArchiveError, "no safe regular"):
            archive_choice_evidence(
                sensitive_root, self.output, repository_root=self.repository
            )
        self.assertFalse(self.output.exists())

    def test_cli_requires_both_explicit_roots(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])
        parsed = parser.parse_args(
            ["--source-root", str(self.source), "--output-root", str(self.output)]
        )
        self.assertEqual(parsed.source_root, self.source)
        self.assertEqual(parsed.output_root, self.output)


if __name__ == "__main__":
    unittest.main()
