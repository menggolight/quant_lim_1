"""Create fail-closed Technical Momentum pre-Locked reports.

The command accepts only Development and Validation split names.  A Locked
split is rejected before config, evidence, or metric paths are opened.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from research.strategy_workspace.technical_formal_reporting import (
    ALLOWED_SPLITS,
    DEFAULT_CONFIG_PATH,
    TechnicalFormalReportingError,
    build_dataset_coverage_report,
    build_development_validation_report,
    build_locked_test_readiness,
    load_and_validate_experiment_config,
    publish_formal_reports,
)


def _locked_split_requested(argv: Sequence[str]) -> bool:
    for index, token in enumerate(argv):
        if token == "--split" and index + 1 < len(argv):
            value = argv[index + 1]
        elif token.startswith("--split="):
            value = token.partition("=")[2]
        else:
            continue
        if "locked" in value.strip().casefold():
            return True
    return False


def _load_optional_json(path: Path | None, label: str) -> Mapping[str, Any] | None:
    if path is None:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = item
            return result

        value = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {item}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TechnicalFormalReportingError(f"unable to load {label}") from exc
    if not isinstance(value, dict):
        raise TechnicalFormalReportingError(f"{label} root must be an object")
    return value


def _generated_at(value: str | None) -> datetime | str | None:
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--dataset-evidence", type=Path)
    parser.add_argument(
        "--split",
        action="append",
        choices=ALLOWED_SPLITS,
        help="May be repeated; omission selects both Development and Validation.",
    )
    parser.add_argument("--development-metrics", type=Path)
    parser.add_argument("--validation-metrics", type=Path)
    parser.add_argument("--generated-at")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if _locked_split_requested(raw_args):
        sys.stderr.write(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": "locked_test_forbidden_before_data_path_read",
                    "locked_test_status": "NOT_RUN",
                    "locked_test_consumed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 2

    parser = build_parser()
    try:
        args = parser.parse_args(raw_args)
        selected = tuple(args.split or ALLOWED_SPLITS)
        if args.output_directory.exists():
            raise TechnicalFormalReportingError("create_only_output_directory_exists")
        experiment = load_and_validate_experiment_config(args.config)
        dataset_evidence = _load_optional_json(
            args.dataset_evidence, "dataset evidence"
        )
        dataset_manifest = build_dataset_coverage_report(
            experiment=experiment,
            dataset_evidence=dataset_evidence,
            generated_at=_generated_at(args.generated_at),
        )

        split_results: dict[str, Mapping[str, Any]] = {}
        # Metric files are intentionally not opened when the formal dataset is
        # blocked.  A blocked data gate cannot consume or interpret results.
        if dataset_manifest["data_status"] == "READY":
            metric_paths = {
                "development": args.development_metrics,
                "validation": args.validation_metrics,
            }
            for split in selected:
                value = _load_optional_json(
                    metric_paths[split], f"{split} metrics"
                )
                if value is not None:
                    split_results[split] = value

        backtest_report = build_development_validation_report(
            experiment=experiment,
            dataset_manifest=dataset_manifest,
            split_results=split_results,
            selected_splits=selected,
            generated_at=_generated_at(args.generated_at),
        )
        readiness = build_locked_test_readiness(
            dataset_manifest=dataset_manifest,
            backtest_report=backtest_report,
            generated_at=_generated_at(args.generated_at),
        )
        paths = publish_formal_reports(
            output_directory=args.output_directory,
            dataset_manifest=dataset_manifest,
            backtest_report=backtest_report,
            readiness_report=readiness,
        )
    except (TechnicalFormalReportingError, OSError) as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                    "locked_test_status": "NOT_RUN",
                    "locked_test_consumed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 2

    sys.stdout.write(
        json.dumps(
            {
                "status": "completed",
                "verdict": readiness["verdict"],
                "locked_test_status": readiness["locked_test_status"],
                "locked_test_consumed": readiness["locked_test_consumed"],
                "output_directory": str(args.output_directory.resolve()),
                "artifacts": {
                    name: str(path.resolve()) for name, path in sorted(paths.items())
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if readiness["verdict"] == "DATA_READY_FOR_LOCKED_TEST" else 1


if __name__ == "__main__":
    raise SystemExit(main())
