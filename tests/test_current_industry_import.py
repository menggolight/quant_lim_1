from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from agent.current_industry_import import (
    CurrentIndustryImportError,
    LEGACY_IMPORTER_SOURCE_SHA256,
    LEGACY_IMPORT_VERSION,
    LEGACY_MANIFEST_VERSION,
    _make_receipt,
    _membership,
    freeze_controlled_sample,
    import_current_industry,
    verify_controlled_sample,
    verify_current_industry,
)
from agent.current_universe_import import import_current_membership
from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.validation import validate_json_schema
from research.strategy_workspace.contracts import canonical_sha256
from tests.test_current_universe_import import _choice_xlsx


ROOT = Path(__file__).resolve().parents[1]
FIXED_CLOCK = lambda: datetime(2026, 8, 19, 4, 0, tzinfo=timezone.utc)
INDUSTRIES = (
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
)


def _inline(reference: str, value: str, *, formula: bool = False) -> str:
    formula_xml = "<f>1+1</f>" if formula else ""
    return (
        f'<c t="inlineStr" r="{reference}">{formula_xml}<is><t>'
        f"{escape(value)}</t></is></c>"
    )


def _number(reference: str, value: str) -> str:
    return f'<c r="{reference}"><v>{value}</v></c>'


def _industry_xlsx(
    path: Path,
    *,
    name_mismatch: bool = False,
    formula: bool = False,
    missing_industry: bool = False,
    omit_taxonomy: bool = False,
    bad_numeric: str | None = None,
) -> None:
    snapshot = "2026-08-18"
    headers = (
        "证券代码",
        "证券名称",
        f"开盘价\n[交易日期]{snapshot}\n[复权方式]前复权",
        f"最高价\n[交易日期]{snapshot}\n[复权方式]前复权",
        f"最低价\n[交易日期]{snapshot}\n[复权方式]前复权",
        f"收盘价\n[交易日期]{snapshot}\n[复权方式]前复权",
        f"前收盘价\n[交易日期]{snapshot}\n[复权方式]不复权",
        f"成交量\n[交易日期]{snapshot}\n[单位]股",
        f"成交额\n[交易日期]{snapshot}\n[单位]元",
        f"涨停价\n[交易日期]{snapshot}",
        f"跌停价\n[交易日期]{snapshot}",
        f"交易状态\n[交易日期]{snapshot}",
        "是否为ST股票\n[截止日期]最新",
        "上市日期",
        f"流通市值\n[交易日期]{snapshot}\n[单位]元",
        "所属中证行业名称(2021)\n[行业类别]1级",
    )
    columns = tuple(chr(ord("A") + index) for index in range(16))
    rows = [
        '<row r="1">'
        + "".join(_inline(f"{column}1", value) for column, value in zip(columns, headers))
        + "</row>"
    ]
    for index in range(800):
        row = index + 2
        name = "错误名称" if name_mismatch and index == 0 else f"证券{index}"
        industry = (
            ""
            if missing_industry and index == 0
            else ("工业" if omit_taxonomy else INDUSTRIES[index % len(INDUSTRIES)])
        )
        cells = [
            _inline(f"A{row}", f"{index:06d}.SZ"),
            _inline(f"B{row}", name),
            _number(f"C{row}", bad_numeric if bad_numeric is not None and index == 0 else "10"),
            _number(f"D{row}", "11"),
            _number(f"E{row}", "9"),
            _number(f"F{row}", "10.5"),
            (
                f'<c r="G{row}"><f>1+1</f><v>10</v></c>'
                if formula and index == 0
                else _number(f"G{row}", "10")
            ),
            _number(f"H{row}", "1000000"),
            _number(f"I{row}", "10000000"),
            _number(f"J{row}", "11.5"),
            _number(f"K{row}", "8.5"),
            _inline(f"L{row}", "正常交易"),
            _inline(f"M{row}", "否"),
            _inline(f"N{row}", "--"),
            _number(f"O{row}", "1000000000"),
        ]
        if industry:
            cells.append(_inline(f"P{row}", industry))
        rows.append(f'<row r="{row}">{"".join(cells)}</row>')
    rows.append(
        '<row r="807"><c t="inlineStr" r="A807"><is><t>'
        "数据来源：妙想Choice</t></is></c></row>"
    )
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="中证800成份" sheetId="1" r:id="rId2"/></sheets></workbook>'''
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(rows)}</sheetData></worksheet>'
    )
    entries = {
        "[Content_Types].xml": b'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>''',
        "docProps/core.xml": b'''<?xml version="1.0" encoding="UTF-8"?><coreProperties xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"/>''',
        "docProps/app.xml": b'''<?xml version="1.0" encoding="UTF-8"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"/>''',
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/styles.xml": b'''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>''',
        "xl/_rels/workbook.xml.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>''',
        "xl/workbook.xml": workbook.encode("utf-8"),
        "xl/sharedStrings.xml": b'''<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>''',
        "xl/worksheets/sheet1.xml": sheet.encode("utf-8"),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


class CurrentIndustryImportTests(unittest.TestCase):
    def _membership(self, root: Path) -> Path:
        source = root / "membership.xlsx"
        output = root / "membership"
        _choice_xlsx(source)
        import_current_membership(
            source,
            output,
            received_date=date(2026, 8, 19),
            clock=FIXED_CLOCK,
        )
        return output

    def test_import_replay_and_freeze_exact_controlled_sample(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            membership = self._membership(root)
            source = root / "industry.xlsx"
            industry = root / "industry"
            sample_dir = root / "sample"
            _industry_xlsx(source)
            manifest = import_current_industry(
                source,
                membership,
                industry,
                received_date=date(2026, 8, 19),
                clock=FIXED_CLOCK,
            )
            self.assertEqual(manifest, verify_current_industry(industry, membership))
            receipt = json.loads(
                (industry / "industry_receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["member_count"], 800)
            self.assertEqual(len(receipt["mappings"]), 800)
            self.assertEqual(receipt["validation"]["listing_date_missing_count"], 800)
            self.assertIsNone(receipt["industry_effective_date"])
            self.assertFalse(receipt["source_authenticated"])
            self.assertFalse(receipt["historical_pit_proven"])
            self.assertFalse(receipt["safety"]["paper_eligibility"])
            schema_path = ROOT / "schemas" / "strategy_current_industry_receipt.v2.json"
            schema = json.loads(
                schema_path.read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(set(receipt), set(schema["properties"]))
            validate_json_schema(receipt, schema_path)
            self.assertEqual(receipt["schema_version"], "strategy-workspace-current-industry-receipt.v2")
            self.assertEqual(set(receipt["validation"]["industry_counts"]), set(INDUSTRIES))

            sample_manifest = freeze_controlled_sample(membership, industry, sample_dir)
            self.assertEqual(
                sample_manifest,
                verify_controlled_sample(membership, industry, sample_dir),
            )
            sample = json.loads((sample_dir / "sample.json").read_text(encoding="utf-8"))
            self.assertEqual(len(sample["instrument_ids"]), 60)
            self.assertEqual(sample["status"], "diagnostic_current_universe_not_pit")
            self.assertFalse(sample["safety"]["paper_eligibility"])
            self.assertFalse(sample["safety"]["real_money_list_allowed"])
            self.assertEqual(
                sample["information_cutoff_date"],
                "2026-08-19",
            )
            self.assertEqual(sample["market_snapshot_date"], "2026-08-18")
            self.assertEqual(
                sample["representation"],
                "diagnostic_equal_industry_coverage_not_csi800_representative",
            )
            self.assertEqual(
                set(sample["industry_by_instrument"]), set(sample["instrument_ids"])
            )
            validate_json_schema(
                sample,
                ROOT / "schemas" / "strategy_current_universe_diagnostic.v2.json",
            )
            with self.assertRaisesRegex(CurrentIndustryImportError, "overwrite"):
                freeze_controlled_sample(membership, industry, sample_dir)

    def test_rejects_member_mismatch_formula_and_missing_industry(self) -> None:
        for kwargs, message in (
            ({"name_mismatch": True}, "verified membership"),
            ({"formula": True}, "forbidden f"),
            ({"missing_industry": True}, "contiguous and complete"),
            ({"omit_taxonomy": True}, "all 11"),
            ({"bad_numeric": "1E3"}, "locked format"),
            ({"bad_numeric": "10000001"}, "diagnostic field limit"),
            ({"bad_numeric": "0.123456789012345678901234567890123"}, "locked format"),
        ):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory(
                dir=ROOT / ".tmp"
            ) as directory:
                root = Path(directory)
                membership = self._membership(root)
                source = root / "industry.xlsx"
                _industry_xlsx(source, **kwargs)
                with self.assertRaisesRegex(CurrentIndustryImportError, message):
                    import_current_industry(
                        source,
                        membership,
                        root / "industry",
                        received_date=date(2026, 8, 19),
                        clock=FIXED_CLOCK,
                    )

    def test_received_date_cannot_postdate_shanghai_import_date(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            membership = self._membership(root)
            source = root / "industry.xlsx"
            _industry_xlsx(source)
            with self.assertRaisesRegex(CurrentIndustryImportError, "later than imported_at"):
                import_current_industry(
                    source,
                    membership,
                    root / "industry",
                    received_date=date(2026, 8, 20),
                    clock=FIXED_CLOCK,
                )

    def test_replays_both_allowlisted_v1_industry_hashes_read_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            membership_dir = self._membership(root)
            membership_receipt, membership_bytes = _membership(membership_dir)
            source = root / "industry.xlsx"
            _industry_xlsx(source)
            content = source.read_bytes()
            for index, importer_hash in enumerate(sorted(LEGACY_IMPORTER_SOURCE_SHA256)):
                with self.subTest(importer_hash=importer_hash):
                    output = root / f"legacy-industry-{index}"
                    output.mkdir()
                    receipt = _make_receipt(
                        content,
                        source_file_name=source.name,
                        received_date=date(2026, 8, 19),
                        imported_at=FIXED_CLOCK(),
                        membership_receipt=membership_receipt,
                        membership_bytes=membership_bytes,
                        legacy_importer_source_sha256=importer_hash,
                    )
                    receipt_bytes = canonical_json_bytes(receipt)
                    manifest = {
                        "schema_version": LEGACY_MANIFEST_VERSION,
                        "import_version": LEGACY_IMPORT_VERSION,
                        "received_date": "2026-08-19",
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
                    (output / "source.xlsx").write_bytes(content)
                    (output / "industry_receipt.json").write_bytes(receipt_bytes)
                    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest))
                    self.assertEqual(
                        verify_current_industry(output, membership_dir)["schema_version"],
                        LEGACY_MANIFEST_VERSION,
                    )

    def test_resigned_sample_cannot_change_frozen_selection_or_safety(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            membership = self._membership(root)
            source = root / "industry.xlsx"
            industry = root / "industry"
            sample_dir = root / "sample"
            _industry_xlsx(source)
            import_current_industry(
                source,
                membership,
                industry,
                received_date=date(2026, 8, 19),
                clock=FIXED_CLOCK,
            )
            freeze_controlled_sample(membership, industry, sample_dir)
            sample_path = sample_dir / "sample.json"
            manifest_path = sample_dir / "manifest.json"
            sample = json.loads(sample_path.read_text(encoding="utf-8"))
            sample["safety"]["paper_eligibility"] = True
            sample.pop("sample_payload_sha256")
            sample["sample_payload_sha256"] = canonical_sha256(sample)
            sample_bytes = canonical_json_bytes(sample)
            sample_path.write_bytes(sample_bytes)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["sample.json"] = sha256_bytes(sample_bytes)
            manifest["sample_artifact_sha256"] = sha256_bytes(sample_bytes)
            manifest["sample_payload_sha256"] = sample["sample_payload_sha256"]
            manifest.pop("manifest_payload_sha256")
            manifest["manifest_payload_sha256"] = canonical_sha256(manifest)
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            with self.assertRaisesRegex(CurrentIndustryImportError, "byte-for-byte"):
                verify_controlled_sample(membership, industry, sample_dir)

    def test_is_append_only_and_replay_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            membership = self._membership(root)
            source = root / "industry.xlsx"
            industry = root / "industry"
            _industry_xlsx(source)
            import_current_industry(
                source,
                membership,
                industry,
                received_date=date(2026, 8, 19),
                clock=FIXED_CLOCK,
            )
            with self.assertRaisesRegex(CurrentIndustryImportError, "overwrite"):
                import_current_industry(
                    source,
                    membership,
                    industry,
                    received_date=date(2026, 8, 19),
                    clock=FIXED_CLOCK,
                )
            (industry / "industry_receipt.json").write_bytes(b"{}")
            with self.assertRaisesRegex(CurrentIndustryImportError, "artifact hashes"):
                verify_current_industry(industry, membership)

    def test_resigned_manifest_cannot_promote_diagnostic_industry(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            membership = self._membership(root)
            source = root / "industry.xlsx"
            industry = root / "industry"
            _industry_xlsx(source)
            import_current_industry(
                source,
                membership,
                industry,
                received_date=date(2026, 8, 19),
                clock=FIXED_CLOCK,
            )
            manifest_path = industry / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("manifest_sha256")
            manifest["schema_version"] = "choice-current-csi800-industry-manifest-v999"
            manifest["status"] = "paper_admitted"
            manifest["formal_truth_eligible"] = True
            manifest["safety"] = {
                "paper_eligibility": True,
                "trade_eligibility": True,
                "real_money_list_allowed": True,
                "live": "enabled",
            }
            manifest["manifest_sha256"] = canonical_sha256(manifest)
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            with self.assertRaisesRegex(CurrentIndustryImportError, "manifest fields"):
                verify_current_industry(industry, membership)


if __name__ == "__main__":
    unittest.main()
