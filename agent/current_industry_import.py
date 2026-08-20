"""Import a Choice current-CSI800 industry snapshot for diagnostics only.

The source workbook is the exact 16-column Choice terminal export inspected on
2026-08-19.  The importer binds its 800 codes to an already verified current
membership import, archives the original bytes, and emits a replayable industry
receipt.  It never claims historical/PIT industry truth or Paper eligibility.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence
import unicodedata
from xml.etree import ElementTree

from agent.current_universe_import import (
    CHINA_TZ,
    CurrentUniverseImportError,
    SOURCE_FOOTER,
    SOURCE_UNIVERSE_ID,
    SOURCE_UNIVERSE_NAME,
    _archive_payloads,
    _artifact_bytes,
    _inside_repository,
    _load_object_bytes,
    _read_stable_regular_file,
    _safe_xml,
    _sheet_name_and_target,
    _validated_source_file_name,
    _verified_current_membership,
)
from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.strategy_workspace.contracts import canonical_sha256
from research.strategy_workspace.diagnostic import (
    CurrentUniverseMember,
    DiagnosticContractError,
    freeze_current_universe_sample,
)


IMPORT_VERSION = "choice-current-csi800-industry-import-v2"
RECEIPT_VERSION = "strategy-workspace-current-industry-receipt.v2"
MANIFEST_VERSION = "choice-current-csi800-industry-manifest-v2"
LEGACY_IMPORT_VERSION = "choice-current-csi800-industry-import-v1"
LEGACY_RECEIPT_VERSION = "strategy-workspace-current-industry-receipt.v1"
LEGACY_MANIFEST_VERSION = "choice-current-csi800-industry-manifest-v1"
LEGACY_IMPORTER_SOURCE_SHA256 = frozenset(
    {
        "c9eb3563a8683104f211c3c4eb69308b0bb69cdd547a349df87f451570b75767",
        "b6323f66eb44d4fb8001ac05f984e786398aa474ceb5bbf69b36949ba38ac250",
    }
)
LEGACY_RECEIPT_SCHEMA_SHA256 = (
    "0f6f3cd95c498fe9b5ecb9d692739d0bb4f98de51a52e2f0c98e004c5d21939d"
)
SAMPLE_MANIFEST_VERSION = "choice-current-csi800-diagnostic-sample-manifest-v2"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MEMBER_COUNT = 800
EXPECTED_COLUMNS = tuple(chr(ord("A") + index) for index in range(16))
INDUSTRY_TAXONOMY = frozenset(
    {
        "能源",
        "原材料",
        "工业",
        "可选消费",
        "主要消费",
        "医药卫生",
        "金融",
        "信息技术",
        "通信服务",
        "公用事业",
        "房地产",
    }
)
_CODE_BUNDLE_PATHS = (
    "agent/current_industry_import.py",
    "agent/current_universe_import.py",
    "research/market_data/contracts.py",
    "research/strategy_workspace/contracts.py",
    "research/strategy_workspace/diagnostic.py",
)

_XML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CELL_REFERENCE = re.compile(r"^([A-Z]+)([1-9]\d*)$")
_INSTRUMENT = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_DATED_HEADER = re.compile(r"\[交易日期\](\d{4}-\d{2}-\d{2})")
_NUMERIC_TEXT = re.compile(
    r"^(?:0|[1-9]\d*)(?:\.\d{1,32})?$"
)


class CurrentIndustryImportError(RuntimeError):
    """Fail-closed error for the diagnostic industry-workbook boundary."""


def _translate_error(exc: Exception) -> CurrentIndustryImportError:
    return CurrentIndustryImportError(str(exc))


def _code_bundle() -> dict[str, Any]:
    files = {
        relative: sha256_bytes(_read_stable_regular_file(REPOSITORY_ROOT / relative))
        for relative in _CODE_BUNDLE_PATHS
    }
    import sys

    runtime = (
        f"{sys.implementation.name}-"
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return {
        "files": files,
        "runtime": runtime,
        "sha256": canonical_sha256({"files": files, "runtime": runtime}),
    }


def _shared_strings(payloads: Mapping[str, bytes]) -> tuple[str, ...]:
    root = _safe_xml(payloads["xl/sharedStrings.xml"], "sharedStrings.xml")
    return tuple(
        "".join(node.text or "" for node in item.findall(f".//{{{_XML_NS}}}t"))
        for item in root.findall(f"{{{_XML_NS}}}si")
    )


def _cell_value(cell: ElementTree.Element, shared: Sequence[str]) -> str | None:
    if cell.find(f"{{{_XML_NS}}}f") is not None:
        raise CurrentIndustryImportError("industry workbook cannot contain formulas")
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(
            item.text or "" for item in cell.findall(f".//{{{_XML_NS}}}t")
        )
    value = cell.find(f"{{{_XML_NS}}}v")
    if value is None:
        return None
    text = value.text or ""
    if kind == "s":
        try:
            return shared[int(text)]
        except (ValueError, IndexError) as exc:
            raise CurrentIndustryImportError("shared-string reference is invalid") from exc
    if kind in {None, "n", "str"}:
        return text
    raise CurrentIndustryImportError("industry workbook contains an unsupported cell type")


def _rows(content: bytes) -> tuple[dict[int, dict[str, str]], dict[str, Any]]:
    try:
        payloads = _archive_payloads(content)
        sheet_name, sheet_target = _sheet_name_and_target(payloads)
    except CurrentUniverseImportError as exc:
        raise _translate_error(exc) from exc
    if sheet_name != SOURCE_UNIVERSE_NAME:
        raise CurrentIndustryImportError("workbook sheet is not the exact Choice CSI800 sheet")
    sheet = _safe_xml(payloads[sheet_target], "sheet1.xml")
    for tag in ("f", "hyperlink", "drawing", "legacyDrawing", "oleObject", "mergeCell"):
        if sheet.find(f".//{{{_XML_NS}}}{tag}") is not None:
            raise CurrentIndustryImportError(f"industry workbook contains forbidden {tag}")
    for row in sheet.findall(f".//{{{_XML_NS}}}row"):
        if row.get("hidden") in {"1", "true"}:
            raise CurrentIndustryImportError("industry workbook contains a hidden row")
    for column in sheet.findall(f".//{{{_XML_NS}}}col"):
        if column.get("hidden") in {"1", "true"}:
            raise CurrentIndustryImportError("industry workbook contains a hidden column")

    shared = _shared_strings(payloads)
    parsed: dict[int, dict[str, str]] = {}
    for row in sheet.findall(f".//{{{_XML_NS}}}row"):
        try:
            row_number = int(str(row.get("r") or ""))
        except ValueError as exc:
            raise CurrentIndustryImportError("industry workbook row reference is invalid") from exc
        if row_number in parsed:
            raise CurrentIndustryImportError("industry workbook repeats a row reference")
        values: dict[str, str] = {}
        for cell in row.findall(f"{{{_XML_NS}}}c"):
            match = _CELL_REFERENCE.fullmatch(str(cell.get("r") or ""))
            if match is None or int(match.group(2)) != row_number:
                raise CurrentIndustryImportError("industry workbook cell reference is invalid")
            column = match.group(1)
            value = _cell_value(cell, shared)
            if value in {None, ""}:
                continue
            if column not in EXPECTED_COLUMNS or column in values:
                raise CurrentIndustryImportError("industry workbook has an unexpected cell column")
            values[column] = value
        parsed[row_number] = values
    return parsed, {
        "sheet_count": 1,
        "sheet_name": sheet_name,
        "formula_count": 0,
        "external_link_count": 0,
    }


def _expected_headers(snapshot_date: date) -> tuple[str, ...]:
    day = snapshot_date.isoformat()
    return (
        "证券代码",
        "证券名称",
        f"开盘价\n[交易日期]{day}\n[复权方式]前复权",
        f"最高价\n[交易日期]{day}\n[复权方式]前复权",
        f"最低价\n[交易日期]{day}\n[复权方式]前复权",
        f"收盘价\n[交易日期]{day}\n[复权方式]前复权",
        f"前收盘价\n[交易日期]{day}\n[复权方式]不复权",
        f"成交量\n[交易日期]{day}\n[单位]股",
        f"成交额\n[交易日期]{day}\n[单位]元",
        f"涨停价\n[交易日期]{day}",
        f"跌停价\n[交易日期]{day}",
        f"交易状态\n[交易日期]{day}",
        "是否为ST股票\n[截止日期]最新",
        "上市日期",
        f"流通市值\n[交易日期]{day}\n[单位]元",
        "所属中证行业名称(2021)\n[行业类别]1级",
    )


def _number(
    value: str,
    label: str,
    *,
    strictly_positive: bool,
    maximum: Decimal,
) -> Decimal:
    if len(value) > 64 or _NUMERIC_TEXT.fullmatch(value) is None:
        raise CurrentIndustryImportError(f"{label} numeric text is outside the locked format")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise CurrentIndustryImportError(f"{label} is not numeric") from exc
    if not number.is_finite() or (number <= 0 if strictly_positive else number < 0):
        raise CurrentIndustryImportError(f"{label} is outside the accepted range")
    numeric_tuple = number.as_tuple()
    if number > maximum or numeric_tuple.exponent < -32 or len(numeric_tuple.digits) > 40:
        raise CurrentIndustryImportError(f"{label} exceeds the diagnostic field limit")
    return number


def _parse_snapshot(
    content: bytes,
    *,
    received_date: date,
    membership_by_id: Mapping[str, str],
) -> tuple[tuple[dict[str, Any], ...], date, dict[str, Any]]:
    rows, structure = _rows(content)
    header = rows.get(1, {})
    if set(header) != set(EXPECTED_COLUMNS):
        raise CurrentIndustryImportError("industry workbook header width differs from the contract")
    dated = {
        match.group(1)
        for value in header.values()
        if (match := _DATED_HEADER.search(value)) is not None
    }
    if len(dated) != 1:
        raise CurrentIndustryImportError("industry workbook mixes snapshot dates")
    snapshot_date = date.fromisoformat(next(iter(dated)))
    if snapshot_date > received_date:
        raise CurrentIndustryImportError("industry snapshot date is later than received_date")
    expected = _expected_headers(snapshot_date)
    actual = tuple(header[column] for column in EXPECTED_COLUMNS)
    if actual != expected:
        raise CurrentIndustryImportError("industry workbook headers differ from the locked Choice profile")

    mappings: list[dict[str, Any]] = []
    for row_number in range(2, EXPECTED_MEMBER_COUNT + 2):
        values = rows.get(row_number)
        if values is None or set(values) != set(EXPECTED_COLUMNS):
            raise CurrentIndustryImportError("industry rows must be contiguous and complete")
        instrument_id = values["A"]
        security_name = values["B"]
        if _INSTRUMENT.fullmatch(instrument_id) is None:
            raise CurrentIndustryImportError("instrument code lacks an explicit SH/SZ suffix")
        if membership_by_id.get(instrument_id) != security_name:
            raise CurrentIndustryImportError("industry workbook differs from the verified membership")
        if len(security_name) > 128 or security_name != unicodedata.normalize(
            "NFC", security_name
        ) or any(
            unicodedata.category(character) in {"Cc", "Cf"} for character in security_name
        ):
            raise CurrentIndustryImportError("security name contains non-canonical control text")
        for column, label in zip("CDEFG", ("open", "high", "low", "close", "preclose")):
            _number(
                values[column],
                label,
                strictly_positive=True,
                maximum=Decimal("10000000"),
            )
        for column, label, maximum in (
            ("H", "volume", Decimal("1000000000000000")),
            ("I", "amount", Decimal("1000000000000000000")),
            ("J", "upper_limit", Decimal("10000000")),
            ("K", "lower_limit", Decimal("10000000")),
            ("O", "float_cap", Decimal("1000000000000000000")),
        ):
            _number(
                values[column],
                label,
                strictly_positive=False,
                maximum=maximum,
            )
        trading_status = values["L"]
        if (
            not trading_status
            or len(trading_status) > 32
            or trading_status != trading_status.strip()
            or trading_status != unicodedata.normalize("NFC", trading_status)
            or any(unicodedata.category(character) in {"Cc", "Cf"} for character in trading_status)
            or values["M"] not in {"是", "否"}
        ):
            raise CurrentIndustryImportError("trading/ST status is incomplete")
        listing_date = values["N"]
        if listing_date != "--":
            try:
                parsed_listing_date = date.fromisoformat(listing_date)
            except ValueError as exc:
                raise CurrentIndustryImportError("listing date is not ISO-8601 or missing") from exc
            if parsed_listing_date > snapshot_date:
                raise CurrentIndustryImportError("listing date cannot postdate the market snapshot")
        industry_name = values["P"]
        if industry_name not in INDUSTRY_TAXONOMY:
            raise CurrentIndustryImportError("industry name is outside CSI 2021 level 1")
        mappings.append(
            {
                "instrument_id": instrument_id,
                "security_name": security_name,
                "industry_id": f"CSI2021_L1/{industry_name}",
                "industry_name": industry_name,
                "source_row": row_number,
            }
        )
    if len({item["instrument_id"] for item in mappings}) != EXPECTED_MEMBER_COUNT:
        raise CurrentIndustryImportError("industry workbook contains duplicate instruments")
    extra = [
        (row_number, values)
        for row_number, values in sorted(rows.items())
        if row_number > EXPECTED_MEMBER_COUNT + 1 and any(values.values())
    ]
    if extra != [(807, {"A": SOURCE_FOOTER})]:
        raise CurrentIndustryImportError("industry workbook footer differs from the Choice export")
    industry_counts: dict[str, int] = {}
    for item in mappings:
        name = item["industry_name"]
        industry_counts[name] = industry_counts.get(name, 0) + 1
    if set(industry_counts) != set(INDUSTRY_TAXONOMY):
        raise CurrentIndustryImportError("industry workbook must represent all 11 CSI level-1 industries")
    structure.update(
        {
            "template_id": "choice_terminal_current_csi800_market_industry_v1",
            "headers": list(expected),
            "source_footer_verified": True,
            "source_footer_cell": "A807",
            "source_footer_sha256": sha256_bytes(SOURCE_FOOTER.encode("utf-8")),
            "member_count": EXPECTED_MEMBER_COUNT,
            "unique_instrument_count": EXPECTED_MEMBER_COUNT,
            "snapshot_date": snapshot_date.isoformat(),
            "industry_counts": dict(sorted(industry_counts.items())),
            "listing_date_missing_count": sum(rows[row]["N"] == "--" for row in range(2, 802)),
        }
    )
    return tuple(sorted(mappings, key=lambda item: item["instrument_id"])), snapshot_date, structure


def _membership(output_dir: Path | str) -> tuple[dict[str, Any], bytes]:
    try:
        _, receipt, raw = _verified_current_membership(output_dir)
    except CurrentUniverseImportError as exc:
        raise _translate_error(exc) from exc
    members = receipt.get("members")
    if not isinstance(members, list) or len(members) != EXPECTED_MEMBER_COUNT:
        raise CurrentIndustryImportError("verified membership receipt is incomplete")
    return receipt, raw


def _make_receipt(
    content: bytes,
    *,
    source_file_name: str,
    received_date: date,
    imported_at: datetime,
    membership_receipt: Mapping[str, Any],
    membership_bytes: bytes,
    legacy_importer_source_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(imported_at, datetime) or imported_at.tzinfo is None:
        raise CurrentIndustryImportError("imported_at must be timezone-aware")
    if received_date > imported_at.astimezone(CHINA_TZ).date():
        raise CurrentIndustryImportError("received_date cannot be later than imported_at")
    membership_received = date.fromisoformat(str(membership_receipt.get("received_date") or ""))
    if membership_received > received_date:
        raise CurrentIndustryImportError("membership receipt cannot postdate industry receipt")
    source_file_name = _validated_source_file_name(source_file_name)
    legacy = legacy_importer_source_sha256 is not None
    if legacy and legacy_importer_source_sha256 not in LEGACY_IMPORTER_SOURCE_SHA256:
        raise CurrentIndustryImportError("legacy industry importer hash is not allowlisted")
    bundle = None if legacy else _code_bundle()
    membership_by_id = {
        str(item["instrument_id"]): str(item["security_name"])
        for item in membership_receipt["members"]
    }
    mappings, snapshot_date, validation = _parse_snapshot(
        content,
        received_date=received_date,
        membership_by_id=membership_by_id,
    )
    as_of = received_date.isoformat()
    industry_by_instrument = {
        item["instrument_id"]: item["industry_id"] for item in mappings
    }
    payload: dict[str, Any] = {
        "schema_version": LEGACY_RECEIPT_VERSION if legacy else RECEIPT_VERSION,
        "import_version": LEGACY_IMPORT_VERSION if legacy else IMPORT_VERSION,
        "source_kind": "choice_terminal_xlsx_export",
        "source_file_name": source_file_name,
        "source_file_sha256": sha256_bytes(content),
        "source_file_size": len(content),
        "received_date": as_of,
        "imported_at": imported_at.astimezone(timezone.utc).isoformat(),
        "market_snapshot_date": snapshot_date.isoformat(),
        "industry_effective_date": None,
        "membership_basis": "current_not_pit",
        "source_universe_id": SOURCE_UNIVERSE_ID,
        "source_universe_name": SOURCE_UNIVERSE_NAME,
        "classification_system": "CSI_2021",
        "classification_level": 1,
        "member_count": EXPECTED_MEMBER_COUNT,
        "mappings": list(mappings),
        "normalized_industry_sha256": canonical_sha256(mappings),
        "current_industry_content_sha256": canonical_sha256(
            {
                "as_of": as_of,
                "source_universe_id": SOURCE_UNIVERSE_ID,
                "industry_by_instrument": industry_by_instrument,
            }
        ),
        "source_membership_artifact_sha256": sha256_bytes(membership_bytes),
        (
            "source_membership_receipt_sha256"
            if legacy
            else "source_membership_payload_sha256"
        ): membership_receipt["receipt_sha256"],
        "source_membership_content_sha256": membership_receipt[
            "current_membership_content_sha256"
        ],
        "workbook_structure_sha256": canonical_sha256(validation),
        "importer_source_sha256": (
            legacy_importer_source_sha256
            if legacy
            else bundle["files"]["agent/current_industry_import.py"]
        ),
        "receipt_schema_sha256": (
            LEGACY_RECEIPT_SCHEMA_SHA256
            if legacy
            else sha256_bytes(
                _read_stable_regular_file(
                    REPOSITORY_ROOT
                    / "schemas"
                    / "strategy_current_industry_receipt.v2.json"
                )
            )
        ),
        "validation": validation,
        "admission_status": "diagnostic_current_industry_only",
        "source_authenticated": False,
        "industry_effective_at_proven": False,
        "historical_pit_proven": False,
        "formal_truth_eligible": False,
        "limitations": [
            "industry_field_has_no_effective_date",
            "current_industry_cannot_backfill_history",
            "st_field_is_latest_not_historical",
            "listing_date_is_missing_in_source",
            "workbook_hash_does_not_prove_official_origin",
        ],
        "safety": {
            "paper_eligibility": False,
            "trade_eligibility": False,
            "real_money_list_allowed": False,
            "live": "not_supported",
        },
    }
    if not legacy:
        payload.update(
            {
                "code_bundle_files": bundle["files"],
                "code_bundle_runtime": bundle["runtime"],
                "code_bundle_sha256": bundle["sha256"],
            }
        )
    payload["receipt_sha256"] = canonical_sha256(payload)
    return payload


def import_current_industry(
    source: Path | str,
    membership_dir: Path | str,
    output_dir: Path | str,
    *,
    received_date: date,
    clock: Any | None = None,
) -> Mapping[str, Any]:
    output = _inside_repository(output_dir, label="output_dir")
    if output.exists():
        raise CurrentIndustryImportError("refusing to overwrite an existing import directory")
    try:
        content = _read_stable_regular_file(Path(source))
    except CurrentUniverseImportError as exc:
        raise _translate_error(exc) from exc
    membership_receipt, membership_bytes = _membership(membership_dir)
    imported_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(imported_at, datetime) or imported_at.tzinfo is None:
        raise CurrentIndustryImportError("import clock must return a timezone-aware datetime")
    receipt = _make_receipt(
        content,
        source_file_name=Path(source).name,
        received_date=received_date,
        imported_at=imported_at,
        membership_receipt=membership_receipt,
        membership_bytes=membership_bytes,
    )
    receipt_bytes = canonical_json_bytes(receipt)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "import_version": IMPORT_VERSION,
        "received_date": received_date.isoformat(),
        "status": "diagnostic_current_industry_only",
        "artifacts": {
            "source.xlsx": sha256_bytes(content),
            "industry_receipt.json": sha256_bytes(receipt_bytes),
        },
        "membership_artifact_sha256": sha256_bytes(membership_bytes),
        "receipt_sha256": receipt["receipt_sha256"],
        "formal_truth_eligible": False,
        "safety": dict(receipt["safety"]),
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".current-industry-", dir=output.parent) as temp:
        staging = Path(temp) / "run"
        staging.mkdir()
        (staging / "source.xlsx").write_bytes(content)
        (staging / "industry_receipt.json").write_bytes(receipt_bytes)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        try:
            staging.rename(output)
        except OSError as exc:
            raise CurrentIndustryImportError("unable to publish import directory") from exc
    return manifest


def _verified_current_industry(
    output_dir: Path | str,
    membership_dir: Path | str,
) -> tuple[dict[str, Any], dict[str, Any], bytes, dict[str, Any], bytes]:
    try:
        _, artifacts = _artifact_bytes(
            output_dir,
            expected_names=frozenset({"source.xlsx", "industry_receipt.json", "manifest.json"}),
            label="industry import",
        )
    except CurrentUniverseImportError as exc:
        raise _translate_error(exc) from exc
    source_bytes = artifacts["source.xlsx"]
    receipt_bytes = artifacts["industry_receipt.json"]
    manifest = _load_object_bytes(artifacts["manifest.json"], "industry manifest")
    if canonical_json_bytes(manifest) != artifacts["manifest.json"]:
        raise CurrentIndustryImportError("industry manifest is not canonical UTF-8 JSON")
    declared_manifest = manifest.pop("manifest_sha256", None)
    if declared_manifest != canonical_sha256(manifest):
        raise CurrentIndustryImportError("industry manifest_sha256 mismatch")
    manifest["manifest_sha256"] = declared_manifest
    if manifest.get("artifacts") != {
        "source.xlsx": sha256_bytes(source_bytes),
        "industry_receipt.json": sha256_bytes(receipt_bytes),
    }:
        raise CurrentIndustryImportError("industry manifest artifact hashes mismatch")
    membership_receipt, membership_bytes = _membership(membership_dir)
    receipt = _load_object_bytes(receipt_bytes, "industry receipt")
    if canonical_json_bytes(receipt) != receipt_bytes:
        raise CurrentIndustryImportError("industry receipt is not canonical UTF-8 JSON")
    declared_receipt = receipt.pop("receipt_sha256", None)
    if declared_receipt != canonical_sha256(receipt):
        raise CurrentIndustryImportError("industry receipt_sha256 mismatch")
    receipt["receipt_sha256"] = declared_receipt
    if manifest.get("membership_artifact_sha256") != sha256_bytes(membership_bytes):
        raise CurrentIndustryImportError("industry manifest membership binding mismatch")
    try:
        received_date = date.fromisoformat(str(receipt.get("received_date") or ""))
        imported_at = datetime.fromisoformat(str(receipt.get("imported_at") or ""))
    except ValueError as exc:
        raise CurrentIndustryImportError("industry receipt date metadata is invalid") from exc
    if imported_at.tzinfo is None:
        raise CurrentIndustryImportError("industry receipt imported_at must be timezone-aware")
    receipt_version = receipt.get("schema_version")
    import_version = receipt.get("import_version")
    if (receipt_version, import_version) == (LEGACY_RECEIPT_VERSION, LEGACY_IMPORT_VERSION):
        legacy_hash = str(receipt.get("importer_source_sha256") or "")
        if legacy_hash not in LEGACY_IMPORTER_SOURCE_SHA256:
            raise CurrentIndustryImportError("legacy industry importer hash is not allowlisted")
        if receipt.get("receipt_schema_sha256") != LEGACY_RECEIPT_SCHEMA_SHA256:
            raise CurrentIndustryImportError("legacy industry schema hash mismatch")
        replayed = _make_receipt(
            source_bytes,
            source_file_name=str(receipt.get("source_file_name") or ""),
            received_date=received_date,
            imported_at=imported_at,
            membership_receipt=membership_receipt,
            membership_bytes=membership_bytes,
            legacy_importer_source_sha256=legacy_hash,
        )
        manifest_version = LEGACY_MANIFEST_VERSION
        expected_import_version = LEGACY_IMPORT_VERSION
    elif (receipt_version, import_version) == (RECEIPT_VERSION, IMPORT_VERSION):
        replayed = _make_receipt(
            source_bytes,
            source_file_name=str(receipt.get("source_file_name") or ""),
            received_date=received_date,
            imported_at=imported_at,
            membership_receipt=membership_receipt,
            membership_bytes=membership_bytes,
        )
        manifest_version = MANIFEST_VERSION
        expected_import_version = IMPORT_VERSION
    else:
        raise CurrentIndustryImportError("industry receipt version is unsupported")
    if canonical_json_bytes(receipt) != canonical_json_bytes(replayed):
        raise CurrentIndustryImportError("industry receipt differs from source replay")
    if manifest.get("receipt_sha256") != receipt["receipt_sha256"]:
        raise CurrentIndustryImportError("industry manifest receipt binding mismatch")
    expected_manifest: dict[str, Any] = {
        "schema_version": manifest_version,
        "import_version": expected_import_version,
        "received_date": receipt["received_date"],
        "status": "diagnostic_current_industry_only",
        "artifacts": {
            "source.xlsx": sha256_bytes(source_bytes),
            "industry_receipt.json": sha256_bytes(receipt_bytes),
        },
        "membership_artifact_sha256": sha256_bytes(membership_bytes),
        "receipt_sha256": receipt["receipt_sha256"],
        "formal_truth_eligible": False,
        "safety": dict(receipt["safety"]),
    }
    expected_manifest["manifest_sha256"] = canonical_sha256(expected_manifest)
    if canonical_json_bytes(manifest) != canonical_json_bytes(expected_manifest):
        raise CurrentIndustryImportError(
            "industry manifest fields differ from the source-replayed contract"
        )
    return manifest, receipt, receipt_bytes, membership_receipt, membership_bytes


def verify_current_industry(
    output_dir: Path | str,
    membership_dir: Path | str,
) -> Mapping[str, Any]:
    manifest, _, _, _, _ = _verified_current_industry(output_dir, membership_dir)
    return manifest


def _controlled_sample(
    membership_receipt: Mapping[str, Any],
    membership_bytes: bytes,
    industry_receipt: Mapping[str, Any],
    industry_bytes: bytes,
):
    mappings = industry_receipt.get("mappings")
    if not isinstance(mappings, list) or len(mappings) != EXPECTED_MEMBER_COUNT:
        raise CurrentIndustryImportError("verified industry receipt mappings are incomplete")
    members = tuple(
        CurrentUniverseMember(str(item["instrument_id"]), str(item["industry_id"]))
        for item in mappings
    )
    bundle = _code_bundle()
    return freeze_current_universe_sample(
        members,
        information_cutoff_date=date.fromisoformat(str(industry_receipt["received_date"])),
        market_snapshot_date=date.fromisoformat(str(industry_receipt["market_snapshot_date"])),
        source_universe_id=SOURCE_UNIVERSE_ID,
        source_membership_artifact_sha256=sha256_bytes(membership_bytes),
        source_membership_payload_sha256=str(membership_receipt["receipt_sha256"]),
        source_membership_content_sha256=str(
            membership_receipt["current_membership_content_sha256"]
        ),
        source_industry_artifact_sha256=sha256_bytes(industry_bytes),
        source_industry_payload_sha256=str(industry_receipt["receipt_sha256"]),
        source_industry_content_sha256=str(industry_receipt["current_industry_content_sha256"]),
        generator_code_bundle_files=bundle["files"],
        generator_code_bundle_runtime=bundle["runtime"],
        generator_code_bundle_sha256=bundle["sha256"],
        sample_size=60,
        historical_pit_proven=False,
    )


def _sample_manifest(
    sample_payload: Mapping[str, Any],
    sample_bytes: bytes,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": SAMPLE_MANIFEST_VERSION,
        "status": "diagnostic_current_universe_not_pit",
        "artifacts": {"sample.json": sha256_bytes(sample_bytes)},
        "sample_artifact_sha256": sha256_bytes(sample_bytes),
        "sample_payload_sha256": sample_payload["sample_payload_sha256"],
        "sample_content_sha256": sample_payload["sample_content_sha256"],
        "source_membership_artifact_sha256": sample_payload[
            "source_membership_artifact_sha256"
        ],
        "source_membership_payload_sha256": sample_payload[
            "source_membership_payload_sha256"
        ],
        "source_membership_content_sha256": sample_payload[
            "source_membership_content_sha256"
        ],
        "source_industry_artifact_sha256": sample_payload[
            "source_industry_artifact_sha256"
        ],
        "source_industry_payload_sha256": sample_payload["source_industry_payload_sha256"],
        "source_industry_content_sha256": sample_payload["source_industry_content_sha256"],
        "formal_truth_eligible": False,
        "safety": dict(sample_payload["safety"]),
    }
    manifest["manifest_payload_sha256"] = canonical_sha256(manifest)
    return manifest


def freeze_controlled_sample(
    membership_dir: Path | str,
    industry_dir: Path | str,
    output_dir: Path | str,
) -> Mapping[str, Any]:
    _, industry, industry_bytes, membership, membership_bytes = _verified_current_industry(
        industry_dir, membership_dir
    )
    sample_payload = _controlled_sample(
        membership, membership_bytes, industry, industry_bytes
    ).to_dict()
    sample_bytes = canonical_json_bytes(sample_payload)
    manifest = _sample_manifest(sample_payload, sample_bytes)
    output = _inside_repository(output_dir, label="sample output_dir")
    if output.exists():
        raise CurrentIndustryImportError("refusing to overwrite an existing sample directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".current-sample-", dir=output.parent) as temp:
        staging = Path(temp) / "run"
        staging.mkdir()
        (staging / "sample.json").write_bytes(sample_bytes)
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        try:
            staging.rename(output)
        except OSError as exc:
            raise CurrentIndustryImportError("unable to publish sample directory") from exc
    return manifest


def verify_controlled_sample(
    membership_dir: Path | str,
    industry_dir: Path | str,
    sample_dir: Path | str,
) -> Mapping[str, Any]:
    _, industry, industry_bytes, membership, membership_bytes = _verified_current_industry(
        industry_dir, membership_dir
    )
    try:
        _, artifacts = _artifact_bytes(
            sample_dir,
            expected_names=frozenset({"sample.json", "manifest.json"}),
            label="diagnostic sample",
        )
    except CurrentUniverseImportError as exc:
        raise _translate_error(exc) from exc
    sample_bytes = artifacts["sample.json"]
    manifest_bytes = artifacts["manifest.json"]
    sample_payload = _load_object_bytes(sample_bytes, "diagnostic sample")
    manifest = _load_object_bytes(manifest_bytes, "diagnostic sample manifest")
    if canonical_json_bytes(sample_payload) != sample_bytes:
        raise CurrentIndustryImportError("diagnostic sample is not canonical UTF-8 JSON")
    if canonical_json_bytes(manifest) != manifest_bytes:
        raise CurrentIndustryImportError("diagnostic sample manifest is not canonical UTF-8 JSON")
    declared_manifest = manifest.pop("manifest_payload_sha256", None)
    if declared_manifest != canonical_sha256(manifest):
        raise CurrentIndustryImportError("diagnostic sample manifest payload hash mismatch")
    manifest["manifest_payload_sha256"] = declared_manifest
    replayed_payload = _controlled_sample(
        membership, membership_bytes, industry, industry_bytes
    ).to_dict()
    replayed_bytes = canonical_json_bytes(replayed_payload)
    if sample_bytes != replayed_bytes:
        raise CurrentIndustryImportError("diagnostic sample differs byte-for-byte from source replay")
    expected_manifest = _sample_manifest(replayed_payload, replayed_bytes)
    if manifest_bytes != canonical_json_bytes(expected_manifest):
        raise CurrentIndustryImportError("diagnostic sample manifest differs from source replay")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import")
    importer.add_argument("--source", type=Path, required=True)
    importer.add_argument("--membership-dir", type=Path, required=True)
    importer.add_argument("--received-date", type=date.fromisoformat, required=True)
    importer.add_argument("--output-dir", type=Path, required=True)
    verifier = subparsers.add_parser("verify")
    verifier.add_argument("--membership-dir", type=Path, required=True)
    verifier.add_argument("--output-dir", type=Path, required=True)
    freezer = subparsers.add_parser("freeze-sample")
    freezer.add_argument("--membership-dir", type=Path, required=True)
    freezer.add_argument("--industry-dir", type=Path, required=True)
    freezer.add_argument("--output-dir", "--output", dest="output_dir", type=Path, required=True)
    sample_verifier = subparsers.add_parser("verify-sample")
    sample_verifier.add_argument("--membership-dir", type=Path, required=True)
    sample_verifier.add_argument("--industry-dir", type=Path, required=True)
    sample_verifier.add_argument("--sample-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            result = import_current_industry(
                args.source,
                args.membership_dir,
                args.output_dir,
                received_date=args.received_date,
            )
        elif args.command == "verify":
            result = verify_current_industry(args.output_dir, args.membership_dir)
        elif args.command == "freeze-sample":
            result = freeze_controlled_sample(
                args.membership_dir,
                args.industry_dir,
                args.output_dir,
            )
        else:
            result = verify_controlled_sample(
                args.membership_dir,
                args.industry_dir,
                args.sample_dir,
            )
    except (
        CurrentIndustryImportError,
        CurrentUniverseImportError,
        DiagnosticContractError,
    ) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
