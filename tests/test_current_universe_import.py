from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from agent.current_universe_import import (
    CurrentUniverseImportError,
    LEGACY_IMPORTER_SOURCE_SHA256,
    LEGACY_IMPORT_VERSION,
    LEGACY_MANIFEST_VERSION,
    _receipt,
    import_current_membership,
    verify_current_membership,
)
from research.market_data.contracts import canonical_json_bytes, sha256_bytes
from research.market_data.validation import validate_json_schema
from research.strategy_workspace.contracts import canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
FIXED_CLOCK = lambda: datetime(2026, 8, 19, 3, 30, tzinfo=timezone.utc)


def _choice_xlsx(
    path: Path,
    *,
    count: int = 800,
    duplicate: bool = False,
    formula: bool = False,
    hidden_sheet: bool = False,
    extra_entry: bool = False,
    external_relationship: bool = False,
    utf16_xml: bool = False,
    dtd_entity: bool = False,
    footer: str = "数据来源：妙想Choice",
) -> None:
    workbook_state = ' state="hidden"' if hidden_sheet else ""
    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="中证800成份" sheetId="1" r:id="rId2"{workbook_state}/></sheets></workbook>'''
    rows = [
        '<row r="1"><c t="inlineStr" r="A1"><is><t>证券代码</t></is></c>'
        '<c t="inlineStr" r="B1"><is><t>证券名称</t></is></c></row>'
    ]
    for index in range(count):
        row = index + 2
        code_index = 0 if duplicate and index == 1 else index
        code = f"{code_index:06d}.SZ"
        formula_xml = "<f>1+1</f>" if formula and index == 0 else ""
        rows.append(
            f'<row r="{row}"><c t="inlineStr" r="A{row}">{formula_xml}'
            f'<is><t>{code}</t></is></c><c t="inlineStr" r="B{row}"><is>'
            f'<t>{escape(f"证券{index}")}</t></is></c></row>'
        )
    rows.append(
        f'<row r="807"><c t="inlineStr" r="A807"><is><t>{escape(footer)}</t></is></c></row>'
    )
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{''.join(rows)}</sheetData></worksheet>'''
    entries = {
        "[Content_Types].xml": b'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>''',
        "docProps/core.xml": b'''<?xml version="1.0" encoding="UTF-8"?><coreProperties xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"/>''',
        "docProps/app.xml": b'''<?xml version="1.0" encoding="UTF-8"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"/>''',
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/styles.xml": b'''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>''',
        "xl/_rels/workbook.xml.rels": (
            '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"'''
            + (' TargetMode="External"' if external_relationship else "")
            + '''/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>'''
        ).encode("utf-8"),
        "xl/workbook.xml": workbook.encode("utf-8"),
        "xl/sharedStrings.xml": b'''<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>''',
        "xl/worksheets/sheet1.xml": sheet.encode("utf-8"),
    }
    if extra_entry:
        entries["xl/externalLinks/externalLink1.xml"] = b"external"
    if utf16_xml:
        entries["docProps/core.xml"] = (
            '<?xml version="1.0" encoding="UTF-16"?><coreProperties '
            'xmlns="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"/>'
        ).encode("utf-16")
    if dtd_entity:
        entries["[Content_Types].xml"] = b'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE Types [<!ENTITY injected "x">]><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'''
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


class CurrentUniverseImportTests(unittest.TestCase):
    def test_import_and_replay_real_shape_never_promote_membership(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "中证800成份.xlsx"
            output = root / "run"
            _choice_xlsx(source)
            manifest = import_current_membership(
                source,
                output,
                received_date=date(2026, 8, 19),
                clock=FIXED_CLOCK,
            )
            verified = verify_current_membership(output)
            self.assertEqual(manifest, verified)
            receipt = json.loads(
                (output / "membership_receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["member_count"], 800)
            self.assertIsNone(receipt["membership_effective_date"])
            self.assertFalse(receipt["source_authenticated"])
            self.assertFalse(receipt["capabilities"]["industry_mapping"])
            self.assertFalse(receipt["historical_pit_proven"])
            self.assertFalse(receipt["safety"]["paper_eligibility"])
            schema_path = ROOT / "schemas" / "strategy_current_membership_receipt.v2.json"
            schema = json.loads(
                schema_path.read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(set(receipt), set(schema["properties"]))
            validate_json_schema(receipt, schema_path)
            self.assertEqual(receipt["schema_version"], "strategy-workspace-current-membership-receipt.v2")
            self.assertEqual(manifest["schema_version"], "choice-current-csi800-membership-manifest-v2")
            self.assertEqual(
                set(receipt["code_bundle_files"]),
                {
                    "agent/current_universe_import.py",
                    "research/market_data/contracts.py",
                    "research/strategy_workspace/contracts.py",
                },
            )

    def test_import_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            output = root / "run"
            _choice_xlsx(source)
            import_current_membership(
                source, output, received_date=date(2026, 8, 19), clock=FIXED_CLOCK
            )
            with self.assertRaisesRegex(CurrentUniverseImportError, "overwrite"):
                import_current_membership(
                    source, output, received_date=date(2026, 8, 19), clock=FIXED_CLOCK
                )

    def test_rejects_incomplete_or_duplicate_membership(self) -> None:
        for kwargs, message in (({"count": 799}, "contiguous"), ({"duplicate": True}, "duplicate")):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory(
                dir=ROOT / ".tmp"
            ) as directory:
                root = Path(directory)
                source = root / "source.xlsx"
                _choice_xlsx(source, **kwargs)
                with self.assertRaisesRegex(CurrentUniverseImportError, message):
                    import_current_membership(
                        source,
                        root / "run",
                        received_date=date(2026, 8, 19),
                        clock=FIXED_CLOCK,
                    )

    def test_rejects_formula_hidden_sheet_and_external_link(self) -> None:
        cases = (
            ({"formula": True}, "forbidden f"),
            ({"hidden_sheet": True}, "hidden"),
            ({"extra_entry": True}, "profile"),
            ({"external_relationship": True}, "external relationship"),
            ({"utf16_xml": True}, "UTF-8 XML"),
            ({"dtd_entity": True}, "forbidden XML declaration"),
        )
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory(
                dir=ROOT / ".tmp"
            ) as directory:
                root = Path(directory)
                source = root / "source.xlsx"
                _choice_xlsx(source, **kwargs)
                with self.assertRaisesRegex(CurrentUniverseImportError, message):
                    import_current_membership(
                        source,
                        root / "run",
                        received_date=date(2026, 8, 19),
                        clock=FIXED_CLOCK,
                    )

    def test_received_date_cannot_postdate_shanghai_import_date(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            _choice_xlsx(source)
            with self.assertRaisesRegex(CurrentUniverseImportError, "later than imported_at"):
                import_current_membership(
                    source,
                    root / "run",
                    received_date=date(2026, 8, 20),
                    clock=FIXED_CLOCK,
                )

    def test_replays_both_allowlisted_v1_importer_hashes_read_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            _choice_xlsx(source)
            content = source.read_bytes()
            for index, importer_hash in enumerate(sorted(LEGACY_IMPORTER_SOURCE_SHA256)):
                with self.subTest(importer_hash=importer_hash):
                    output = root / f"legacy-{index}"
                    output.mkdir()
                    receipt = _receipt(
                        content,
                        source_file_name=source.name,
                        received_date=date(2026, 8, 19),
                        imported_at=FIXED_CLOCK(),
                        legacy_importer_source_sha256=importer_hash,
                    )
                    receipt_bytes = canonical_json_bytes(receipt)
                    manifest = {
                        "schema_version": LEGACY_MANIFEST_VERSION,
                        "import_version": LEGACY_IMPORT_VERSION,
                        "received_date": "2026-08-19",
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
                    (output / "source.xlsx").write_bytes(content)
                    (output / "membership_receipt.json").write_bytes(receipt_bytes)
                    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest))
                    self.assertEqual(
                        verify_current_membership(output)["schema_version"],
                        LEGACY_MANIFEST_VERSION,
                    )

    def test_rejects_footer_drift(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            _choice_xlsx(source, footer="数据来源：未知")
            with self.assertRaisesRegex(CurrentUniverseImportError, "footer"):
                import_current_membership(
                    source,
                    root / "run",
                    received_date=date(2026, 8, 19),
                    clock=FIXED_CLOCK,
                )

    def test_replay_rejects_source_or_receipt_tampering(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            output = root / "run"
            _choice_xlsx(source)
            import_current_membership(
                source, output, received_date=date(2026, 8, 19), clock=FIXED_CLOCK
            )
            (output / "source.xlsx").write_bytes(b"tampered")
            with self.assertRaisesRegex(CurrentUniverseImportError, "artifact hashes"):
                verify_current_membership(output)

    def test_resigned_manifest_cannot_promote_diagnostic_membership(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as directory:
            root = Path(directory)
            source = root / "source.xlsx"
            output = root / "run"
            _choice_xlsx(source)
            import_current_membership(
                source, output, received_date=date(2026, 8, 19), clock=FIXED_CLOCK
            )
            manifest_path = output / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("manifest_sha256")
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
            with self.assertRaisesRegex(CurrentUniverseImportError, "manifest fields"):
                verify_current_membership(output)


if __name__ == "__main__":
    unittest.main()
