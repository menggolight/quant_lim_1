import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.market_observation_dashboard import ObservationValidationError, validate_manifest
from agent.market_observation_pipeline import (
    MANIFEST_VERSION,
    PIPELINE_VERSION,
    _git_state,
    run_pipeline,
)
from research.market_data.contracts import MarketDataRequest
from research.market_data.providers.baostock import BaoStockProvider
from research.market_data.registry import MarketDataRegistry
from research.market_data.storage import MarketDataStorage
from research.reproducibility import git_worktree_state

try:
    from test_market_observation_dashboard import (
        draft_observation_fixture,
        encoded_json,
        sealed_observation_fixture,
        standard_manifest,
    )
except ModuleNotFoundError:
    from tests.test_market_observation_dashboard import (
        draft_observation_fixture,
        encoded_json,
        sealed_observation_fixture,
        standard_manifest,
    )


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "market_observation.v0.1.json"


def write_draft(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def default_sealed_at(payload: dict) -> datetime:
    return datetime.fromisoformat(payload["generated_at"]) + timedelta(minutes=5)


def run_at(sealed_at: datetime, **arguments):
    with patch("agent.market_observation_pipeline._now", return_value=sealed_at):
        return run_pipeline(**arguments)


def run_in(root: Path, payload: dict, *, sealed_at: datetime | None = None, **overrides):
    input_path = root / "draft.json"
    write_draft(input_path, payload)
    arguments = {
        "input_path": input_path,
        "previous_path": None,
        "previous_manifest_path": None,
        "first_baseline": True,
        "schema_path": SCHEMA_PATH,
        "signals_dir": root / "signals",
        "manifest_dir": root / "manifests",
        "dashboard_dir": root / "dashboards",
        "workspace": REPOSITORY_ROOT,
    }
    arguments.update(overrides)
    return run_at(sealed_at or default_sealed_at(payload), **arguments)


def write_validated_market_batch(root: Path) -> Path:
    requested_at = datetime.fromisoformat("2026-08-05T09:00:00+08:00")
    fetched_at = datetime.fromisoformat("2026-08-05T10:00:00+08:00")
    request = MarketDataRequest(
        dataset_type="daily_bar",
        requested_at=requested_at,
        retrieval_mode="historical_backfill",
        instrument_id="000333.SZ",
        start_date="2026-08-04",
        end_date="2026-08-04",
        adjustment="none",
    )

    class Result:
        error_code = "0"
        error_msg = ""

        def __init__(self, fields, rows):
            self.fields = fields
            self.rows = rows
            self.index = -1

        def next(self):
            self.index += 1
            return self.index < len(self.rows)

        def get_row_data(self):
            return self.rows[self.index]

    class SDK:
        def login(self):
            return Result([], [])

        def logout(self):
            return Result([], [])

        def query_history_k_data_plus(self, *_args, **_kwargs):
            return Result(
                list(BaoStockProvider._DAILY_FIELDS),
                [[
                    "2026-08-04", "sz.000333", "50", "52", "49.5", "51",
                    "49.8", "100000", "5100000", "3", "1",
                ]],
            )

        def query_trade_dates(self, **_kwargs):
            return Result(
                ["calendar_date", "is_trading_day"],
                [["2026-08-04", "1"]],
            )

    provider = BaoStockProvider(sdk_loader=lambda: SDK(), clock=lambda: fetched_at)
    with patch(
        "research.market_data.providers.BaoStockProvider",
        return_value=provider,
    ):
        registry = MarketDataRegistry.configured(storage_root=root / "market-data")
    storage = registry.storage
    assert storage is not None
    batch = registry.fetch(request, provider_id="baostock")
    key = storage.cache_key(
        batch.provider_id,
        batch.dataset_type,
        batch.request_fingerprint,
        batch.adapter_version,
        batch.schema_version,
    )
    return (
        storage.root
        / "validated"
        / batch.provider_id
        / batch.dataset_type
        / key
        / f"{batch.batch_id}.json"
    )


class MarketObservationPipelineTest(unittest.TestCase):
    def test_first_baseline_seals_observation_manifest_and_dashboards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            draft = draft_observation_fixture()
            outputs = run_in(root, draft)

            sealed = json.loads(outputs.observation_path.read_text(encoding="utf-8"))
            manifest = json.loads(outputs.manifest_path.read_text(encoding="utf-8"))
            sealed_hash = hashlib.sha256(outputs.observation_path.read_bytes()).hexdigest()

            self.assertEqual(sealed["comparison"]["status"], "first_baseline")
            self.assertIsNone(sealed["comparison"]["previous_observation_id"])
            self.assertFalse(sealed["comparison"]["has_material_change"])
            self.assertEqual(sealed["pipeline"]["producer"], PIPELINE_VERSION)
            self.assertTrue(sealed["pipeline"]["standard_cli_generated"])
            self.assertEqual(
                datetime.fromisoformat(sealed["pipeline"]["sealed_at"]),
                default_sealed_at(draft),
            )
            self.assertEqual(manifest["manifest_version"], MANIFEST_VERSION)
            self.assertTrue(manifest["standard_cli_generated"])
            self.assertEqual(manifest["sealed_at"], sealed["pipeline"]["sealed_at"])
            self.assertEqual(manifest["outputs"][0]["role"], "sealed_observation")
            self.assertEqual(Path(manifest["outputs"][0]["path"]).resolve(), outputs.observation_path.resolve())
            self.assertEqual(manifest["outputs"][0]["sha256"], sealed_hash)
            self.assertEqual(manifest["schema"]["schema_version"], sealed["schema_version"])
            self.assertFalse(any(manifest["admission"].values()))
            self.assertEqual(manifest["market_data_batches"], [])
            if manifest["working_tree_dirty_at_generation"] is True:
                self.assertRegex(manifest["git_diff_sha256"], r"^[0-9a-f]{64}$")
            else:
                self.assertIsNone(manifest["git_diff_sha256"])
            self.assertTrue(outputs.snapshot_dashboard_path.exists())
            self.assertTrue(outputs.latest_dashboard_path.exists())
            self.assertTrue(outputs.latest_alias_path.exists())
            self.assertEqual(
                outputs.snapshot_dashboard_path.read_bytes(),
                outputs.latest_dashboard_path.read_bytes(),
            )
            alias = json.loads(outputs.latest_alias_path.read_text(encoding="utf-8"))
            self.assertEqual(alias["alias_version"], "market-observation-latest-alias-v0.1")
            self.assertIs(alias["mutable"], True)
            self.assertEqual(alias["observation_id"], sealed["observation_id"])
            self.assertEqual(alias["sealed_at"], sealed["pipeline"]["sealed_at"])
            alias_paths = {
                "observation": outputs.observation_path,
                "manifest": outputs.manifest_path,
                "snapshot_dashboard": outputs.snapshot_dashboard_path,
                "latest_dashboard": outputs.latest_dashboard_path,
            }
            for role, path in alias_paths.items():
                with self.subTest(alias_role=role):
                    self.assertEqual(Path(alias[role]["path"]).resolve(), path.resolve())
                    self.assertEqual(alias[role]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(alias["latest_dashboard"]["sha256"], alias["snapshot_dashboard"]["sha256"])
            self.assertIn("首次基线 · 暂无前次变化", outputs.latest_dashboard_path.read_text(encoding="utf-8"))
            validate_manifest(outputs.manifest_path, sealed, sealed_hash, outputs.observation_path)

            controlled_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    outputs.observation_path,
                    outputs.manifest_path,
                    outputs.snapshot_dashboard_path,
                    outputs.latest_alias_path,
                )
            }
            repeated = run_in(
                root,
                draft,
                sealed_at=default_sealed_at(draft) + timedelta(hours=3),
            )
            repeated_sealed = json.loads(repeated.observation_path.read_text(encoding="utf-8"))
            repeated_manifest = json.loads(repeated.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(repeated_sealed["pipeline"]["sealed_at"], sealed["pipeline"]["sealed_at"])
            self.assertEqual(repeated_manifest["sealed_at"], manifest["sealed_at"])
            repeated_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    repeated.observation_path,
                    repeated.manifest_path,
                    repeated.snapshot_dashboard_path,
                    repeated.latest_alias_path,
                )
            }
            self.assertEqual(repeated_hashes, controlled_hashes)
            self.assertEqual(
                repeated.snapshot_dashboard_path.read_bytes(),
                repeated.latest_dashboard_path.read_bytes(),
            )

    def test_v03_manifest_binds_validated_batch_raw_hash_and_dashboard_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            batch_path = write_validated_market_batch(root)
            outputs = run_in(
                root,
                draft_observation_fixture(),
                market_data_batch_paths=(batch_path,),
                market_data_storage_root=root / "market-data",
            )
            sealed = json.loads(outputs.observation_path.read_text(encoding="utf-8"))
            manifest = json.loads(outputs.manifest_path.read_text(encoding="utf-8"))
            sealed_hash = hashlib.sha256(outputs.observation_path.read_bytes()).hexdigest()

            self.assertEqual(len(manifest["market_data_batches"]), 1)
            evidence = manifest["market_data_batches"][0]
            self.assertEqual(evidence["provider_id"], "baostock")
            self.assertEqual(
                evidence["upstream_source"],
                "baostock.query_history_k_data_plus",
            )
            self.assertEqual(evidence["admission_status"], "validated_research_only")
            self.assertEqual(evidence["batch_file_sha256"], hashlib.sha256(batch_path.read_bytes()).hexdigest())
            self.assertEqual(
                [item["role"] for item in manifest["inputs"]].count("market_data_batch"),
                1,
            )
            content = outputs.latest_dashboard_path.read_text(encoding="utf-8")
            self.assertIn("结构化市场数据证据", content)
            self.assertIn("baostock.query_history_k_data_plus", content)
            self.assertNotIn("Legacy 历史行情诊断（补充源）", content)
            self.assertNotIn("Legacy 行业榜诊断（补充源）", content)
            with self.assertRaisesRegex(ObservationValidationError, "controlled root"):
                validate_manifest(
                    outputs.manifest_path,
                    sealed,
                    sealed_hash,
                    outputs.observation_path,
                )
            validate_manifest(
                outputs.manifest_path,
                sealed,
                sealed_hash,
                outputs.observation_path,
                market_data_storage_root=root / "market-data",
            )

            storage_root = batch_path.parents[4]
            raw_path = (
                storage_root
                / "raw"
                / evidence["provider_id"]
                / evidence["dataset_type"]
                / batch_path.parent.name
                / f"{evidence['batch_id']}.raw"
            )
            raw_path.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ObservationValidationError, "raw evidence hash"):
                validate_manifest(
                    outputs.manifest_path,
                    sealed,
                    sealed_hash,
                    outputs.observation_path,
                    market_data_storage_root=root / "market-data",
                )

    def test_git_state_hashes_dirty_content_or_reports_clean_state(self):
        commit, dirty, diff_hash = _git_state(REPOSITORY_ROOT)
        self.assertRegex(commit or "", r"^[0-9a-f]{40}$")
        self.assertIsInstance(dirty, bool)
        if dirty:
            self.assertRegex(diff_hash or "", r"^[0-9a-f]{64}$")
        else:
            self.assertIsNone(diff_hash)

    def test_git_state_refuses_head_change_during_clean_snapshot(self):
        responses = [
            SimpleNamespace(stdout="a" * 40 + "\n"),
            SimpleNamespace(stdout=b""),
            SimpleNamespace(stdout="b" * 40 + "\n"),
            SimpleNamespace(stdout=b""),
        ]
        with patch("research.reproducibility.subprocess.run", side_effect=responses):
            self.assertEqual(git_worktree_state(REPOSITORY_ROOT), (None, None, None))

    def test_git_state_refuses_head_change_during_dirty_snapshot(self):
        status = b" M tracked.py\0"
        diff = b"binary diff"
        responses = [
            SimpleNamespace(stdout="a" * 40 + "\n"),
            SimpleNamespace(stdout=status),
            SimpleNamespace(stdout=diff),
            SimpleNamespace(stdout=b""),
            SimpleNamespace(stdout=status),
            SimpleNamespace(stdout=diff),
            SimpleNamespace(stdout=b""),
            SimpleNamespace(stdout="b" * 40 + "\n"),
        ]
        with patch("research.reproducibility.subprocess.run", side_effect=responses):
            self.assertEqual(git_worktree_state(REPOSITORY_ROOT), (None, None, None))

    def test_v03_manifest_rejects_type_confusion_and_missing_evidence_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            outputs = run_in(root, draft_observation_fixture())
            sealed = json.loads(outputs.observation_path.read_text(encoding="utf-8"))
            sealed_hash = hashlib.sha256(outputs.observation_path.read_bytes()).hexdigest()
            original = json.loads(outputs.manifest_path.read_text(encoding="utf-8"))

            mutations = []
            wrong_type = copy.deepcopy(original)
            wrong_type["working_tree_dirty_at_generation"] = "true"
            mutations.append(wrong_type)
            missing_batches = copy.deepcopy(original)
            missing_batches.pop("market_data_batches")
            mutations.append(missing_batches)
            missing_diff_field = copy.deepcopy(original)
            missing_diff_field.pop("git_diff_sha256")
            mutations.append(missing_diff_field)

            for index, manifest in enumerate(mutations):
                path = root / f"invalid-manifest-{index}.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.subTest(index=index), self.assertRaises(ObservationValidationError):
                    validate_manifest(
                        path,
                        sealed,
                        sealed_hash,
                        outputs.observation_path,
                    )

    def test_pipeline_refuses_when_git_state_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "agent.market_observation_pipeline._git_state",
            return_value=(None, None, None),
        ):
            with self.assertRaisesRegex(ObservationValidationError, "Git working-tree"):
                run_in(Path(temp_dir), draft_observation_fixture())

    def test_previous_observation_and_manifest_drive_comparison(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous_outputs = run_in(root, draft_observation_fixture("2026-08-04"))
            previous_hash = hashlib.sha256(previous_outputs.observation_path.read_bytes()).hexdigest()

            current = draft_observation_fixture("2026-08-05")
            current["overall"]["macro_environment"] = "neutral"
            current["macro"]["state"] = "neutral"
            current["industry"]["sectors"][0]["state"] = "neutral_strong"
            current["stock"]["cross_industry_observation_samples"][0]["state"] = "leading"
            current["three_layer_conflicts"] = ["新增冲突"]
            current_input = root / "current-draft.json"
            write_draft(current_input, current)

            outputs = run_at(
                default_sealed_at(current),
                input_path=current_input,
                previous_path=previous_outputs.observation_path,
                previous_manifest_path=previous_outputs.manifest_path,
                first_baseline=False,
                schema_path=SCHEMA_PATH,
                signals_dir=root / "signals",
                manifest_dir=root / "manifests",
                dashboard_dir=root / "dashboards",
                workspace=REPOSITORY_ROOT,
            )

            sealed = json.loads(outputs.observation_path.read_text(encoding="utf-8"))
            comparison = sealed["comparison"]
            self.assertEqual(comparison["status"], "compared")
            self.assertEqual(comparison["previous_observation_id"], "cn-market-2026-08-04-close")
            self.assertEqual(comparison["previous_sha256"], previous_hash)
            self.assertTrue(comparison["has_material_change"])
            self.assertEqual(comparison["overall_state_changes"][0]["field"], "macro_environment")
            self.assertEqual(comparison["industry_state_changes"][0]["subject_id"], "932082")
            self.assertEqual(comparison["stock_state_changes"][0]["subject_id"], "688981.SH")
            self.assertEqual(comparison["new_conflicts"], ["新增冲突"])
            self.assertEqual(comparison["resolved_conflicts"], ["信息技术基本面与价格冲突"])

            manifest = json.loads(outputs.manifest_path.read_text(encoding="utf-8"))
            input_roles = {entry["role"]: entry for entry in manifest["inputs"]}
            self.assertEqual(Path(input_roles["previous_observation"]["path"]).resolve(), previous_outputs.observation_path.resolve())
            self.assertEqual(input_roles["previous_observation"]["sha256"], previous_hash)
            self.assertEqual(Path(input_roles["previous_manifest"]["path"]).resolve(), previous_outputs.manifest_path.resolve())
            self.assertIn("较 2026-08-04 存在状态变化", outputs.latest_dashboard_path.read_text(encoding="utf-8"))
            alias = json.loads(outputs.latest_alias_path.read_text(encoding="utf-8"))
            self.assertEqual(alias["observation_id"], current["observation_id"])
            self.assertEqual(comparison["previous_observation_id"], "cn-market-2026-08-04-close")
            self.assertEqual(comparison["previous_sha256"], previous_hash)

    def test_previous_observation_requires_its_manifest_pair(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous = root / "previous.json"
            write_draft(previous, draft_observation_fixture("2026-08-04"))

            with self.assertRaisesRegex(ObservationValidationError, "must be provided together"):
                run_in(
                    root,
                    draft_observation_fixture("2026-08-05"),
                    previous_path=previous,
                    previous_manifest_path=None,
                    first_baseline=False,
                )

    def test_rejects_forged_comparison_or_pipeline_in_draft(self):
        for field in ("comparison", "pipeline"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temp_dir:
                payload = draft_observation_fixture()
                payload[field] = {"forged": True}
                with self.assertRaisesRegex(ObservationValidationError, "draft must not supply computed"):
                    run_in(Path(temp_dir), payload)

    def test_pipeline_fails_closed_on_trade_action_or_factor_eligibility(self):
        cases = [
            (lambda item: item["overall"].__setitem__("trade_action", "buy"), "trade_action"),
            (lambda item: item["data_quality"].__setitem__("formal_factor_eligibility", True), "formal_factor_eligibility"),
            (lambda item: item["macro"].__setitem__("state", "risk_on"), "macro.state"),
        ]
        for mutate, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                payload = draft_observation_fixture()
                mutate(payload)
                with self.assertRaisesRegex(ObservationValidationError, message):
                    run_in(root, payload)
                self.assertFalse((root / "signals").exists())
                self.assertFalse((root / "manifests").exists())
                self.assertFalse((root / "dashboards").exists())

    def test_controlled_snapshot_and_observation_are_immutable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = draft_observation_fixture()
            snapshot = root / "dashboards" / f"{payload['observation_id']}.html"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text("occupied", encoding="utf-8")

            with self.assertRaisesRegex(ObservationValidationError, "controlled output collision"):
                run_in(root, payload)
            self.assertFalse((root / "signals" / f"{payload['observation_id']}.sealed.json").exists())
            self.assertFalse((root / "manifests" / f"{payload['observation_id']}.manifest.json").exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = draft_observation_fixture()
            outputs = run_in(root, payload)
            original = outputs.observation_path.read_bytes()
            changed = copy.deepcopy(payload)
            changed["purpose"] = "同一观察ID但不同内容"

            with self.assertRaisesRegex(ObservationValidationError, "different draft"):
                run_in(root, changed)
            self.assertEqual(outputs.observation_path.read_bytes(), original)

    def test_latest_dashboard_and_alias_never_regress_or_change_identity_at_same_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            newer = draft_observation_fixture("2026-08-05")
            current = run_in(root, newer)
            latest_before = current.latest_dashboard_path.read_bytes()
            alias_before = current.latest_alias_path.read_bytes()

            with self.assertRaises(ObservationValidationError):
                run_in(root, draft_observation_fixture("2026-08-04"))
            self.assertEqual(current.latest_dashboard_path.read_bytes(), latest_before)
            self.assertEqual(current.latest_alias_path.read_bytes(), alias_before)

            same_decision_other_id = copy.deepcopy(newer)
            same_decision_other_id["observation_id"] = "cn-market-2026-08-05-close-alt"
            with self.assertRaises(ObservationValidationError):
                run_in(root, same_decision_other_id)
            self.assertEqual(current.latest_dashboard_path.read_bytes(), latest_before)
            self.assertEqual(current.latest_alias_path.read_bytes(), alias_before)

    def test_latest_without_alias_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dashboard_dir = root / "dashboards"
            dashboard_dir.mkdir(parents=True)
            latest_path = dashboard_dir / "latest.html"
            latest_path.write_text("unbound latest", encoding="utf-8")

            with self.assertRaises(ObservationValidationError):
                run_in(root, draft_observation_fixture())
            self.assertEqual(latest_path.read_text(encoding="utf-8"), "unbound latest")
            self.assertFalse((dashboard_dir / "latest.alias.json").exists())

    def test_newer_observation_must_chain_from_the_alias_current_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current_alias = run_in(root, draft_observation_fixture("2026-08-04"))
            latest_before = current_alias.latest_dashboard_path.read_bytes()
            alias_before = current_alias.latest_alias_path.read_bytes()

            rogue_root = root / "rogue"
            rogue_previous = draft_observation_fixture("2026-08-04")
            rogue_previous["observation_id"] = "cn-market-2026-08-04-close-alt"
            rogue_outputs = run_in(rogue_root, rogue_previous)
            newer = draft_observation_fixture("2026-08-05")
            newer_input = root / "newer.json"
            write_draft(newer_input, newer)

            with self.assertRaises(ObservationValidationError):
                run_at(
                    default_sealed_at(newer),
                    input_path=newer_input,
                    previous_path=rogue_outputs.observation_path,
                    previous_manifest_path=rogue_outputs.manifest_path,
                    first_baseline=False,
                    schema_path=SCHEMA_PATH,
                    signals_dir=root / "signals",
                    manifest_dir=root / "manifests",
                    dashboard_dir=root / "dashboards",
                    workspace=REPOSITORY_ROOT,
                )
            self.assertEqual(current_alias.latest_dashboard_path.read_bytes(), latest_before)
            self.assertEqual(current_alias.latest_alias_path.read_bytes(), alias_before)

    def test_previous_sealed_at_after_current_decision_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous_path = root / "previous.sealed.json"
            previous = sealed_observation_fixture("2026-08-04")
            previous["pipeline"]["sealed_at"] = "2026-08-05T15:11:00+08:00"
            previous_raw = encoded_json(previous)
            previous_path.write_bytes(previous_raw)
            previous_hash = hashlib.sha256(previous_raw).hexdigest()
            previous_manifest_path = root / "previous.manifest.json"
            previous_manifest_path.write_text(
                json.dumps(standard_manifest(previous_path, previous, previous_hash)),
                encoding="utf-8",
            )
            current = draft_observation_fixture("2026-08-05")
            current_input = root / "current.json"
            write_draft(current_input, current)

            with self.assertRaisesRegex(ObservationValidationError, "sealed_at"):
                run_at(
                    default_sealed_at(current),
                    input_path=current_input,
                    previous_path=previous_path,
                    previous_manifest_path=previous_manifest_path,
                    first_baseline=False,
                    schema_path=SCHEMA_PATH,
                    signals_dir=root / "signals",
                    manifest_dir=root / "manifests",
                    dashboard_dir=root / "dashboards",
                    workspace=REPOSITORY_ROOT,
                )
            self.assertFalse((root / "signals").exists())

    def test_tampered_previous_manifest_is_rejected_before_comparison(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            previous_outputs = run_in(root, draft_observation_fixture("2026-08-04"))
            manifest = json.loads(previous_outputs.manifest_path.read_text(encoding="utf-8"))
            manifest["outputs"][0]["role"] = "draft_observation"
            previous_outputs.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            current_input = root / "current.json"
            current = draft_observation_fixture("2026-08-05")
            write_draft(current_input, current)

            with self.assertRaisesRegex(ObservationValidationError, "sealed_observation output"):
                run_at(
                    default_sealed_at(current),
                    input_path=current_input,
                    previous_path=previous_outputs.observation_path,
                    previous_manifest_path=previous_outputs.manifest_path,
                    first_baseline=False,
                    schema_path=SCHEMA_PATH,
                    signals_dir=root / "signals",
                    manifest_dir=root / "manifests",
                    dashboard_dir=root / "dashboards",
                    workspace=REPOSITORY_ROOT,
                )


if __name__ == "__main__":
    unittest.main()
