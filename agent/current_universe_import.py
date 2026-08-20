"""Import one narrow Choice terminal CSI800 workbook as diagnostic evidence.

The importer deliberately accepts only the simple two-column workbook emitted
by the Choice terminal.  It archives the exact bytes and produces a replayable
membership receipt.  It does not create an industry receipt, prove historical
point-in-time membership, or unlock research/Paper/trading admission.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Mapping, Sequence
import unicodedata
import zipfile
from xml.etree import ElementTree

from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.strategy_workspace.contracts import canonical_sha256


IMPORT_VERSION = "choice-current-csi800-membership-import-v2"
RECEIPT_VERSION = "strategy-workspace-current-membership-receipt.v2"
MANIFEST_VERSION = "choice-current-csi800-membership-manifest-v2"
LEGACY_IMPORT_VERSION = "choice-current-csi800-membership-import-v1"
LEGACY_RECEIPT_VERSION = "strategy-workspace-current-membership-receipt.v1"
LEGACY_MANIFEST_VERSION = "choice-current-csi800-membership-manifest-v1"
LEGACY_IMPORTER_SOURCE_SHA256 = frozenset(
    {
        "1a5bfea4052deda70ed415e7177bbeb85e632b9e4cb38bfcd102ab0cf966be15",
        "58472aee92e8dccb2ca1109a60c1320538ac8385be245dd85b725961e6b7686d",
    }
)
LEGACY_RECEIPT_SCHEMA_SHA256 = (
    "1a2f7d0f0c5984c4797aba9f98e7d8e4eae51aa905170e2b25517d247d756ff1"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_UNIVERSE_ID = "CSI800_CURRENT_CHOICE"
SOURCE_UNIVERSE_NAME = "中证800成份"
SOURCE_FOOTER = "数据来源：妙想Choice"
TEMPLATE_ID = "choice_terminal_current_csi800_two_column_v1"
EXPECTED_HEADERS = ("证券代码", "证券名称")
EXPECTED_MEMBER_COUNT = 800
MAX_XLSX_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

_CODE_BUNDLE_PATHS = (
    "agent/current_universe_import.py",
    "research/market_data/contracts.py",
    "research/strategy_workspace/contracts.py",
)

_INSTRUMENT = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_CELL_REFERENCE = re.compile(r"^([A-Z]+)([1-9]\d*)$")
_XML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_ROOT_RELATIONSHIPS = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
            "docProps/core.xml",
        ),
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
            "docProps/app.xml",
        ),
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
            "xl/workbook.xml",
        ),
    }
)
_WORKBOOK_RELATIONSHIPS = frozenset(
    {
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            "styles.xml",
        ),
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
            "worksheets/sheet1.xml",
        ),
        (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings",
            "sharedStrings.xml",
        ),
    }
)
_EXPECTED_ARCHIVE_ENTRIES = frozenset(
    {
        "[Content_Types].xml",
        "docProps/core.xml",
        "docProps/app.xml",
        "_rels/.rels",
        "xl/styles.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/workbook.xml",
        "xl/sharedStrings.xml",
        "xl/worksheets/sheet1.xml",
    }
)


class CurrentUniverseImportError(RuntimeError):
    """Fail-closed error for the diagnostic workbook boundary."""


def _is_reparse(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _read_stable_regular_file(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CurrentUniverseImportError("unable to inspect source workbook") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        raise CurrentUniverseImportError("source workbook must be a regular non-link file")
    if before.st_size <= 0 or before.st_size > MAX_XLSX_BYTES:
        raise CurrentUniverseImportError("source workbook size is outside the diagnostic limit")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise CurrentUniverseImportError("unable to read source workbook") from exc
    after = path.lstat()
    stable = lambda item: (
        getattr(item, "st_dev", 0),
        getattr(item, "st_ino", 0),
        item.st_size,
        item.st_mtime_ns,
        getattr(item, "st_ctime_ns", 0),
        getattr(item, "st_file_attributes", 0),
    )
    if (
        not stat.S_ISREG(after.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or _is_reparse(after)
        or stable(before) != stable(after)
    ):
        raise CurrentUniverseImportError("source workbook changed during import")
    return content


def _code_bundle() -> dict[str, Any]:
    files = {
        relative: sha256_bytes(_read_stable_regular_file(REPOSITORY_ROOT / relative))
        for relative in _CODE_BUNDLE_PATHS
    }
    runtime = (
        f"{sys.implementation.name}-"
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return {
        "files": files,
        "runtime": runtime,
        "sha256": canonical_sha256({"files": files, "runtime": runtime}),
    }


def _decode_xml(raw: bytes, label: str) -> str:
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise CurrentUniverseImportError(f"{label} must be UTF-8 XML") from exc
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise CurrentUniverseImportError(f"{label} contains a forbidden XML declaration")
    declaration = re.match(r"\s*<\?xml\s+([^?]+)\?>", text, flags=re.IGNORECASE)
    if declaration is not None:
        encoding = re.search(
            r"encoding\s*=\s*['\"]([^'\"]+)['\"]",
            declaration.group(1),
            flags=re.IGNORECASE,
        )
        if encoding is not None and encoding.group(1).replace("-", "").upper() != "UTF8":
            raise CurrentUniverseImportError(f"{label} must declare UTF-8 encoding")
    return text


def _safe_xml(raw: bytes, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(_decode_xml(raw, label))
    except ElementTree.ParseError as exc:
        raise CurrentUniverseImportError(f"{label} is malformed") from exc


def _relationship_map(
    raw: bytes,
    label: str,
    expected: frozenset[tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    root = _safe_xml(raw, label)
    if root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships" or root.attrib:
        raise CurrentUniverseImportError(f"{label} has an unexpected relationships root")
    relationships: dict[str, tuple[str, str]] = {}
    for item in list(root):
        if item.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
            raise CurrentUniverseImportError(f"{label} contains an unexpected element")
        relationship_id = str(item.get("Id") or "")
        relationship_type = str(item.get("Type") or "")
        target = str(item.get("Target") or "")
        if not relationship_id or relationship_id in relationships:
            raise CurrentUniverseImportError(f"{label} contains duplicate relationship ids")
        if item.get("TargetMode") is not None:
            raise CurrentUniverseImportError(f"{label} contains an external relationship")
        if set(item.attrib) != {"Id", "Type", "Target"} or list(item):
            raise CurrentUniverseImportError(f"{label} contains unexpected relationship fields")
        relationships[relationship_id] = (relationship_type, target)
    if frozenset(relationships.values()) != expected or len(relationships) != len(expected):
        raise CurrentUniverseImportError(f"{label} differs from the locked relationship profile")
    return relationships


def _archive_payloads(content: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(BytesIO(content), mode="r") as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise CurrentUniverseImportError("workbook archive contains duplicate paths")
            if set(names) != _EXPECTED_ARCHIVE_ENTRIES:
                raise CurrentUniverseImportError(
                    "workbook archive differs from the narrow Choice export profile"
                )
            total = 0
            payloads: dict[str, bytes] = {}
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts or info.flag_bits & 0x1:
                    raise CurrentUniverseImportError("workbook archive path or encryption is forbidden")
                archive_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(archive_mode) not in {0, stat.S_IFREG}:
                    raise CurrentUniverseImportError("workbook archive contains a non-regular entry")
                total += info.file_size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise CurrentUniverseImportError("workbook archive exceeds the expansion limit")
                if info.file_size > 0 and (
                    info.compress_size == 0 or info.file_size / info.compress_size > 100
                ):
                    raise CurrentUniverseImportError("workbook archive compression ratio is unsafe")
                payloads[info.filename] = archive.read(info)
            for name, raw in payloads.items():
                if name.endswith(".xml") or name.endswith(".rels"):
                    _decode_xml(raw, name)
            _relationship_map(payloads["_rels/.rels"], "_rels/.rels", _ROOT_RELATIONSHIPS)
            return payloads
    except (zipfile.BadZipFile, OSError) as exc:
        raise CurrentUniverseImportError("source is not a readable XLSX archive") from exc


def _sheet_name_and_target(payloads: Mapping[str, bytes]) -> tuple[str, str]:
    workbook = _safe_xml(payloads["xl/workbook.xml"], "workbook.xml")
    sheets = workbook.findall(f".//{{{_XML_NS}}}sheet")
    if len(sheets) != 1:
        raise CurrentUniverseImportError("Choice membership workbook must contain exactly one sheet")
    sheet = sheets[0]
    if sheet.get("state") not in {None, "visible"}:
        raise CurrentUniverseImportError("membership sheet cannot be hidden")
    name = str(sheet.get("name") or "")
    relationship_id = str(sheet.get(f"{{{_REL_NS}}}id") or "")
    relationships = _relationship_map(
        payloads["xl/_rels/workbook.xml.rels"],
        "workbook.xml.rels",
        _WORKBOOK_RELATIONSHIPS,
    )
    relationship = relationships.get(relationship_id)
    if relationship != (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        "worksheets/sheet1.xml",
    ):
        raise CurrentUniverseImportError("workbook relationship does not target sheet1.xml")
    return name, "xl/worksheets/sheet1.xml"


def _cell_text(cell: ElementTree.Element) -> str:
    if cell.find(f"{{{_XML_NS}}}f") is not None:
        raise CurrentUniverseImportError("membership workbook cannot contain formulas")
    if cell.get("t") != "inlineStr":
        raise CurrentUniverseImportError("membership workbook cells must be literal inline text")
    texts = [item.text or "" for item in cell.findall(f".//{{{_XML_NS}}}t")]
    return "".join(texts)


def _parse_membership(content: bytes) -> tuple[tuple[dict[str, str], ...], dict[str, Any]]:
    payloads = _archive_payloads(content)
    sheet_name, sheet_target = _sheet_name_and_target(payloads)
    if sheet_name != SOURCE_UNIVERSE_NAME:
        raise CurrentUniverseImportError("workbook sheet is not the exact Choice CSI800 sheet")
    sheet = _safe_xml(payloads[sheet_target], "sheet1.xml")
    forbidden_tags = ("f", "hyperlink", "drawing", "legacyDrawing", "oleObject", "mergeCell")
    for tag in forbidden_tags:
        if sheet.find(f".//{{{_XML_NS}}}{tag}") is not None:
            raise CurrentUniverseImportError(f"membership workbook contains forbidden {tag}")
    for row in sheet.findall(f".//{{{_XML_NS}}}row"):
        if row.get("hidden") in {"1", "true"}:
            raise CurrentUniverseImportError("membership workbook contains a hidden row")
    for column in sheet.findall(f".//{{{_XML_NS}}}col"):
        if column.get("hidden") in {"1", "true"}:
            raise CurrentUniverseImportError("membership workbook contains a hidden column")

    rows: dict[int, dict[str, str]] = {}
    for row in sheet.findall(f".//{{{_XML_NS}}}row"):
        try:
            row_number = int(str(row.get("r") or ""))
        except ValueError as exc:
            raise CurrentUniverseImportError("membership workbook row reference is invalid") from exc
        if row_number in rows:
            raise CurrentUniverseImportError("membership workbook repeats a row reference")
        values: dict[str, str] = {}
        for cell in row.findall(f"{{{_XML_NS}}}c"):
            match = _CELL_REFERENCE.fullmatch(str(cell.get("r") or ""))
            if match is None or int(match.group(2)) != row_number:
                raise CurrentUniverseImportError("membership workbook cell reference is invalid")
            column = match.group(1)
            if column not in {"A", "B"} or column in values:
                raise CurrentUniverseImportError("membership workbook has an unexpected cell column")
            values[column] = _cell_text(cell)
        rows[row_number] = values

    if (rows.get(1, {}).get("A"), rows.get(1, {}).get("B")) != EXPECTED_HEADERS:
        raise CurrentUniverseImportError("membership workbook headers differ from the Choice contract")
    members: list[dict[str, str]] = []
    for row_number in range(2, EXPECTED_MEMBER_COUNT + 2):
        values = rows.get(row_number)
        if values is None or set(values) != {"A", "B"}:
            raise CurrentUniverseImportError("membership rows must be contiguous and complete")
        instrument_id = values["A"]
        security_name = values["B"]
        if _INSTRUMENT.fullmatch(instrument_id) is None:
            raise CurrentUniverseImportError("instrument code lacks an explicit SH/SZ suffix")
        if (
            not security_name
            or len(security_name) > 128
            or security_name != security_name.strip()
        ):
            raise CurrentUniverseImportError("security name must be non-empty literal text")
        if security_name != unicodedata.normalize("NFC", security_name) or any(
            unicodedata.category(character) in {"Cc", "Cf"} for character in security_name
        ):
            raise CurrentUniverseImportError("security name contains non-canonical control text")
        members.append(
            {
                "instrument_id": instrument_id,
                "security_name": security_name,
                "source_row": row_number,
            }
        )
    extra_content = [
        (row_number, values)
        for row_number, values in sorted(rows.items())
        if row_number > EXPECTED_MEMBER_COUNT + 1 and any(values.values())
    ]
    if extra_content != [(807, {"A": SOURCE_FOOTER})]:
        raise CurrentUniverseImportError("membership workbook footer differs from the Choice export")
    instrument_ids = [item["instrument_id"] for item in members]
    if len(set(instrument_ids)) != EXPECTED_MEMBER_COUNT:
        raise CurrentUniverseImportError("membership workbook contains duplicate instruments")
    return tuple(members), {
        "template_id": TEMPLATE_ID,
        "workbook_profile": "choice_terminal_simple_export_v1",
        "sheet_count": 1,
        "sheet_name": sheet_name,
        "headers": list(EXPECTED_HEADERS),
        "formula_count": 0,
        "external_link_count": 0,
        "source_footer_verified": True,
        "source_footer_cell": "A807",
        "source_footer_sha256": sha256_bytes(SOURCE_FOOTER.encode("utf-8")),
        "member_count": EXPECTED_MEMBER_COUNT,
        "unique_instrument_count": len(set(instrument_ids)),
        "market_counts": {
            "SH": sum(item.endswith(".SH") for item in instrument_ids),
            "SZ": sum(item.endswith(".SZ") for item in instrument_ids),
        },
    }


def _validated_source_file_name(value: str) -> str:
    if (
        not value
        or len(value) > 255
        or value != unicodedata.normalize("NFC", value)
        or value != Path(value).name
        or Path(value).suffix.lower() != ".xlsx"
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise CurrentUniverseImportError("source file name contains unsafe audit text")
    return value


def _receipt(
    content: bytes,
    *,
    source_file_name: str,
    received_date: date,
    imported_at: datetime,
    legacy_importer_source_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(imported_at, datetime) or imported_at.tzinfo is None:
        raise CurrentUniverseImportError("imported_at must be timezone-aware")
    if received_date > imported_at.astimezone(CHINA_TZ).date():
        raise CurrentUniverseImportError("received_date cannot be later than imported_at")
    source_file_name = _validated_source_file_name(source_file_name)
    members, validation = _parse_membership(content)
    instrument_ids = sorted(item["instrument_id"] for item in members)
    legacy = legacy_importer_source_sha256 is not None
    if legacy and legacy_importer_source_sha256 not in LEGACY_IMPORTER_SOURCE_SHA256:
        raise CurrentUniverseImportError("legacy membership importer hash is not allowlisted")
    bundle = None if legacy else _code_bundle()
    payload: dict[str, Any] = {
        "schema_version": LEGACY_RECEIPT_VERSION if legacy else RECEIPT_VERSION,
        "import_version": LEGACY_IMPORT_VERSION if legacy else IMPORT_VERSION,
        "source_kind": "choice_terminal_xlsx_export",
        "source_file_name": source_file_name,
        "source_file_sha256": sha256_bytes(content),
        "source_file_size": len(content),
        "received_date": received_date.isoformat(),
        "imported_at": imported_at.astimezone(timezone.utc).isoformat(),
        "membership_effective_date": None,
        "membership_basis": "current_not_pit",
        "source_universe_id": SOURCE_UNIVERSE_ID,
        "source_universe_name": SOURCE_UNIVERSE_NAME,
        "source_footer": SOURCE_FOOTER,
        "member_count": EXPECTED_MEMBER_COUNT,
        "members": sorted(members, key=lambda item: item["instrument_id"]),
        "normalized_members_sha256": canonical_sha256(
            sorted(members, key=lambda item: item["instrument_id"])
        ),
        "current_membership_content_sha256": canonical_sha256(
            {
                "source_universe_id": SOURCE_UNIVERSE_ID,
                "membership_basis": "current_not_pit",
                "membership_effective_date": None,
                "instrument_ids": instrument_ids,
            }
        ),
        "workbook_structure_sha256": canonical_sha256(validation),
        "importer_source_sha256": (
            legacy_importer_source_sha256
            if legacy
            else bundle["files"]["agent/current_universe_import.py"]
        ),
        "receipt_schema_sha256": (
            LEGACY_RECEIPT_SCHEMA_SHA256
            if legacy
            else sha256_bytes(
                _read_stable_regular_file(
                    REPOSITORY_ROOT
                    / "schemas"
                    / "strategy_current_membership_receipt.v2.json"
                )
            )
        ),
        "validation": validation,
        "admission_status": "diagnostic_current_membership_only",
        "source_authenticated": False,
        "historical_pit_proven": False,
        "formal_truth_eligible": False,
        "capabilities": {
            "current_csi800_membership": True,
            "industry_mapping": False,
            "historical_membership": False,
            "point_in_time_membership": False,
            "total_return_benchmark": False,
            "financials": False,
        },
        "limitations": [
            "received_date_is_not_membership_effective_date",
            "current_membership_cannot_backfill_history",
            "industry_mapping_not_in_workbook",
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


def _inside_repository(path: Path | str, *, label: str) -> Path:
    repository = REPOSITORY_ROOT.resolve()
    candidate = Path(os.path.abspath(Path(path)))
    try:
        relative = candidate.relative_to(repository)
    except ValueError as exc:
        raise CurrentUniverseImportError(f"{label} must stay inside the repository") from exc
    if not relative.parts:
        raise CurrentUniverseImportError(f"{label} cannot be the repository root")
    current = repository
    for part in relative.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        try:
            details = current.lstat()
        except OSError as exc:
            raise CurrentUniverseImportError(f"unable to inspect {label} path") from exc
        if stat.S_ISLNK(details.st_mode) or _is_reparse(details):
            raise CurrentUniverseImportError(f"{label} path cannot contain links or reparse points")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(repository)
    except ValueError as exc:
        raise CurrentUniverseImportError(f"{label} resolved outside the repository") from exc
    return resolved


def import_current_membership(
    source: Path | str,
    output_dir: Path | str,
    *,
    received_date: date,
    clock: Any | None = None,
) -> Mapping[str, Any]:
    source_path = Path(source)
    output = _inside_repository(output_dir, label="output_dir")
    if output.exists():
        raise CurrentUniverseImportError("refusing to overwrite an existing import directory")
    content = _read_stable_regular_file(source_path)
    imported_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(imported_at, datetime) or imported_at.tzinfo is None:
        raise CurrentUniverseImportError("import clock must return a timezone-aware datetime")
    receipt = _receipt(
        content,
        source_file_name=source_path.name,
        received_date=received_date,
        imported_at=imported_at,
    )
    receipt_bytes = canonical_json_bytes(receipt)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_VERSION,
        "import_version": IMPORT_VERSION,
        "received_date": received_date.isoformat(),
        "status": "diagnostic_current_membership_only",
        "artifacts": {
            "source.xlsx": sha256_bytes(content),
            "membership_receipt.json": sha256_bytes(receipt_bytes),
        },
        "receipt_sha256": receipt["receipt_sha256"],
        "formal_truth_eligible": False,
        "safety": dict(receipt["safety"]),
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    manifest_bytes = canonical_json_bytes(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".current-membership-", dir=output.parent) as temp:
        staging = Path(temp) / "run"
        staging.mkdir()
        (staging / "source.xlsx").write_bytes(content)
        (staging / "membership_receipt.json").write_bytes(receipt_bytes)
        (staging / "manifest.json").write_bytes(manifest_bytes)
        try:
            staging.rename(output)
        except OSError as exc:
            raise CurrentUniverseImportError("unable to publish import directory") from exc
    return manifest


def _load_object_bytes(raw: bytes, label: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise CurrentUniverseImportError(f"{label} contains duplicate JSON keys")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(
                CurrentUniverseImportError(f"{label} contains a non-finite JSON number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CurrentUniverseImportError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise CurrentUniverseImportError(f"{label} root must be an object")
    return value


def _load_object(path: Path, label: str) -> dict[str, Any]:
    return _load_object_bytes(_read_stable_regular_file(path), label)


def _artifact_bytes(
    output_dir: Path | str,
    *,
    expected_names: frozenset[str],
    label: str,
) -> tuple[Path, dict[str, bytes]]:
    output = _inside_repository(output_dir, label="output_dir")
    if not output.is_dir():
        raise CurrentUniverseImportError(f"{label} directory is missing")
    before = output.lstat()
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        raise CurrentUniverseImportError(f"{label} must be a regular non-reparse directory")
    entries = tuple(output.iterdir())
    if {path.name for path in entries} != expected_names:
        raise CurrentUniverseImportError(f"{label} artifact set differs from the contract")
    artifacts = {
        name: _read_stable_regular_file(output / name) for name in sorted(expected_names)
    }
    if {path.name for path in output.iterdir()} != expected_names:
        raise CurrentUniverseImportError(f"{label} artifact set changed during verification")
    after = output.lstat()
    identity = lambda item: (
        getattr(item, "st_dev", 0),
        getattr(item, "st_ino", 0),
        item.st_mtime_ns,
        getattr(item, "st_ctime_ns", 0),
        getattr(item, "st_file_attributes", 0),
    )
    if identity(before) != identity(after):
        raise CurrentUniverseImportError(f"{label} directory changed during verification")
    return output, artifacts


def _verified_current_membership(
    output_dir: Path | str,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    _, artifacts = _artifact_bytes(
        output_dir,
        expected_names=frozenset(
            {"source.xlsx", "membership_receipt.json", "manifest.json"}
        ),
        label="membership import",
    )
    source_bytes = artifacts["source.xlsx"]
    receipt_bytes = artifacts["membership_receipt.json"]
    manifest = _load_object_bytes(artifacts["manifest.json"], "manifest")
    if canonical_json_bytes(manifest) != artifacts["manifest.json"]:
        raise CurrentUniverseImportError("manifest is not canonical UTF-8 JSON")
    declared_manifest_sha = manifest.pop("manifest_sha256", None)
    if declared_manifest_sha != canonical_sha256(manifest):
        raise CurrentUniverseImportError("manifest_sha256 mismatch")
    manifest["manifest_sha256"] = declared_manifest_sha
    if manifest.get("artifacts") != {
        "source.xlsx": sha256_bytes(source_bytes),
        "membership_receipt.json": sha256_bytes(receipt_bytes),
    }:
        raise CurrentUniverseImportError("manifest artifact hashes mismatch")
    receipt = _load_object_bytes(receipt_bytes, "membership receipt")
    if canonical_json_bytes(receipt) != receipt_bytes:
        raise CurrentUniverseImportError("membership receipt is not canonical UTF-8 JSON")
    declared_receipt_sha = receipt.pop("receipt_sha256", None)
    if declared_receipt_sha != canonical_sha256(receipt):
        raise CurrentUniverseImportError("receipt_sha256 mismatch")
    receipt["receipt_sha256"] = declared_receipt_sha
    try:
        received_date = date.fromisoformat(str(receipt.get("received_date") or ""))
        imported_at = datetime.fromisoformat(str(receipt.get("imported_at") or ""))
    except ValueError as exc:
        raise CurrentUniverseImportError("receipt date metadata is invalid") from exc
    if imported_at.tzinfo is None:
        raise CurrentUniverseImportError("receipt imported_at must be timezone-aware")
    receipt_version = receipt.get("schema_version")
    import_version = receipt.get("import_version")
    if (receipt_version, import_version) == (LEGACY_RECEIPT_VERSION, LEGACY_IMPORT_VERSION):
        legacy_hash = str(receipt.get("importer_source_sha256") or "")
        if legacy_hash not in LEGACY_IMPORTER_SOURCE_SHA256:
            raise CurrentUniverseImportError("legacy membership importer hash is not allowlisted")
        if receipt.get("receipt_schema_sha256") != LEGACY_RECEIPT_SCHEMA_SHA256:
            raise CurrentUniverseImportError("legacy membership schema hash mismatch")
        replayed = _receipt(
            source_bytes,
            source_file_name=str(receipt.get("source_file_name") or ""),
            received_date=received_date,
            imported_at=imported_at,
            legacy_importer_source_sha256=legacy_hash,
        )
        manifest_version = LEGACY_MANIFEST_VERSION
        expected_import_version = LEGACY_IMPORT_VERSION
    elif (receipt_version, import_version) == (RECEIPT_VERSION, IMPORT_VERSION):
        replayed = _receipt(
            source_bytes,
            source_file_name=str(receipt.get("source_file_name") or ""),
            received_date=received_date,
            imported_at=imported_at,
        )
        manifest_version = MANIFEST_VERSION
        expected_import_version = IMPORT_VERSION
    else:
        raise CurrentUniverseImportError("membership receipt version is unsupported")
    if canonical_json_bytes(receipt) != canonical_json_bytes(replayed):
        raise CurrentUniverseImportError("membership receipt differs from source replay")
    if manifest.get("receipt_sha256") != receipt["receipt_sha256"]:
        raise CurrentUniverseImportError("manifest receipt binding mismatch")
    expected_manifest: dict[str, Any] = {
        "schema_version": manifest_version,
        "import_version": expected_import_version,
        "received_date": receipt["received_date"],
        "status": "diagnostic_current_membership_only",
        "artifacts": {
            "source.xlsx": sha256_bytes(source_bytes),
            "membership_receipt.json": sha256_bytes(receipt_bytes),
        },
        "receipt_sha256": receipt["receipt_sha256"],
        "formal_truth_eligible": False,
        "safety": dict(receipt["safety"]),
    }
    expected_manifest["manifest_sha256"] = canonical_sha256(expected_manifest)
    if canonical_json_bytes(manifest) != canonical_json_bytes(expected_manifest):
        raise CurrentUniverseImportError(
            "manifest fields differ from the source-replayed membership contract"
        )
    return manifest, receipt, receipt_bytes


def verify_current_membership(output_dir: Path | str) -> Mapping[str, Any]:
    manifest, _, _ = _verified_current_membership(output_dir)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import")
    importer.add_argument("--source", type=Path, required=True)
    importer.add_argument("--received-date", type=date.fromisoformat, required=True)
    importer.add_argument("--output-dir", type=Path, required=True)
    verifier = subparsers.add_parser("verify")
    verifier.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = (
            import_current_membership(
                args.source,
                args.output_dir,
                received_date=args.received_date,
            )
            if args.command == "import"
            else verify_current_membership(args.output_dir)
        )
    except CurrentUniverseImportError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
