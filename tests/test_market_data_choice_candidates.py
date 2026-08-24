from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.choice_candidate_probe import build_parser, run_probe
from research.market_data.choice_candidates import (
    ADMISSION_STATUS,
    ChoiceCandidateError,
    ChoiceCandidateEvidence,
    ChoiceCandidateService,
    ChoiceCandidateStorage,
    edb_publish_dates_request,
    replay_candidate_raw,
    sw2021_request,
)
from research.market_data.contracts import canonical_json_bytes
from research.market_data.providers.base import ProviderQueryError
from research.market_data.providers.choice import ChoiceProvider


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
NOW = datetime(2026, 8, 11, 15, 30, tzinfo=CHINA_TZ)


def result(error_code=0, error_message="success", **values):
    defaults = {
        "Codes": [],
        "Indicators": [],
        "Dates": [],
        "Data": [],
    }
    defaults.update(values)
    return SimpleNamespace(
        ErrorCode=error_code,
        ErrorMsg=error_message,
        **defaults,
    )


def css_result(*, code="000333.SZ", indicator="SW2021"):
    return result(
        Codes=[code],
        Indicators=[indicator],
        Data={code: ["家用电器>白色家电>空调"]},
    )


def sector_result(*, members=None):
    members = members or ["000333.SZ", "600000.SH"]
    names = {"000333.SZ": "美的集团", "600000.SH": "浦发银行"}
    flattened = [value for member in members for value in (member, names.get(member, "名称"))]
    return result(
        Codes=members,
        Indicators=["SECUCODE", "SECURITYSHORTNAME"],
        Dates=["2024-06-30"],
        Data=flattened,
    )


def metadata_result(*, code="EMM00087117", indicators=None):
    indicators = indicators or [
        "ID",
        "NAME",
        "UNIT",
        "SOURCE",
        "REGION",
        "FREQUENCY",
        "STARTDATE",
        "ENDDATE",
        "UPDATETIME",
    ]
    values = [
        code,
        "工业增加值",
        "%",
        "聚合来源",
        "中国",
        "5",
        "2020-01-01",
        "2026-07-01",
        "2026-08-01",
    ]
    return result(
        Codes=[code],
        Indicators=indicators,
        Dates=["2026-08-11"],
        Data={code: [[value] for value in values]},
    )


def edb_result(*, code="EMM00087117", indicators=None):
    indicators = indicators or ["VALUE", "PUBLISHDATE"]
    return result(
        Codes=[code],
        Indicators=indicators,
        Dates=["2026-06-01", "2026-07-01"],
        Data={
            code: [
                ["5.8", "6.1"],
                ["2026-07-15", "2026-08-15"],
            ]
        },
    )


class FakeChoiceClient:
    def __init__(
        self,
        *,
        css=None,
        sector=None,
        metadata=None,
        edb=None,
        start=None,
        stop=None,
    ):
        self.css_response = css or css_result()
        self.sector_response = sector or sector_result()
        self.metadata_response = metadata or metadata_result()
        self.edb_response = edb or edb_result()
        self.start_response = start or result()
        self.stop_response = stop or result()
        self.calls = []

    def start(self, *args):
        self.calls.append(("start", args))
        return self.start_response

    def stop(self, *args):
        self.calls.append(("stop", args))
        return self.stop_response

    def css(self, *args):
        self.calls.append(("css", args))
        return self.css_response

    def sector(self, *args):
        self.calls.append(("sector", args))
        return self.sector_response

    def edbquery(self, *args):
        self.calls.append(("edbquery", args))
        return self.metadata_response

    def edb(self, *args):
        self.calls.append(("edb", args))
        return self.edb_response

    def porder(self, *_):
        raise AssertionError("account/portfolio/trading methods must never be called")


class ChoiceCandidateTests(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "research.market_data.provider_access.require_choice_network_access"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self, directory, client):
        return ChoiceCandidateService(
            Path(directory),
            provider=ChoiceProvider(
                sdk_loader=lambda: SimpleNamespace(c=client),
                clock=lambda: NOW,
            ),
        )

    def test_three_explicit_apis_are_diagnostic_only_and_persisted(self):
        client = FakeChoiceClient()
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, client)
            css = service.fetch_sw2021_classification("000333.SZ")
            sector = service.fetch_historical_sector_membership("001004", "2024-06-30")
            edb = service.fetch_edb_publish_dates(["EMM00087117"])
            for evidence in (css, sector, edb):
                self.assertEqual(evidence.status, "passed")
                self.assertEqual(evidence.admission_status, ADMISSION_STATUS)
                self.assertEqual(evidence.point_in_time_status, ADMISSION_STATUS)
                self.assertFalse(evidence.formal_truth_eligible)
                self.assertGreater(evidence.record_count, 0)
            self.assertFalse(css.records[0]["formal_truth_eligible"])
            self.assertFalse(sector.records[0]["historical_pit_proven"])
            self.assertFalse(edb.records[0]["first_release_proven"])
            evidence_files = list(Path(directory).glob("evidence/choice/*/*/*.json"))
            self.assertEqual(len(evidence_files), 3)

        calls = {name: args for name, args in client.calls if name not in {"start", "stop"}}
        self.assertEqual(
            calls["css"],
            ("000333.SZ", "SW2021", "Ispandas=0,RowIndex=1,RECVtimeout=30"),
        )
        self.assertEqual(
            calls["sector"],
            ("001004", "2024-06-30", "Ispandas=0,RowIndex=1,RECVtimeout=30"),
        )
        self.assertEqual(
            calls["edbquery"][1],
            "ID,NAME,UNIT,SOURCE,REGION,FREQUENCY,STARTDATE,ENDDATE,UPDATETIME",
        )
        self.assertIn("IsPublishDate=1", calls["edb"][1])

    def test_10001012_is_not_configured_not_success(self):
        client = FakeChoiceClient(css=result(10001012, "insufficient user access"))
        with tempfile.TemporaryDirectory() as directory:
            evidence = self.service(directory, client).fetch_sw2021_classification(
                "000333.SZ"
            )
            self.assertEqual(evidence.status, "not_configured")
            self.assertEqual(evidence.record_count, 0)
            self.assertFalse(evidence.formal_truth_eligible)
            replayed = self.service(directory, FakeChoiceClient()).replay_sw2021_classification(
                "000333.SZ"
            )
            self.assertEqual(replayed.evidence_id, evidence.evidence_id)
            self.assertEqual(replayed.status, "not_configured")
            _, raw = ChoiceCandidateStorage(directory).load_latest(
                "sw2021_classification", sw2021_request("000333.SZ")
            )
            upstream_error = json.loads(raw.decode("utf-8"))["failure"][
                "rejected_response"
            ]["provider_error"]
            self.assertEqual(upstream_error["error_code"], "10001012")

    def test_wrong_code_indicator_and_unverified_shape_fail_closed(self):
        cases = (
            css_result(code="600000.SH"),
            css_result(indicator="SW"),
            result(
                Codes=["000333.SZ"],
                Indicators=["SW2021"],
                Data={"000333.SZ": [None]},
            ),
            SimpleNamespace(
                ErrorCode=0,
                ErrorMsg="success",
                Codes=["000333.SZ"],
                Indicators=["SW2021"],
                Dates=[],
                # Data deliberately missing.
            ),
        )
        for response in cases:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                evidence = self.service(
                    directory, FakeChoiceClient(css=response)
                ).fetch_sw2021_classification("000333.SZ")
                self.assertEqual(evidence.status, "failed")
                self.assertEqual(evidence.record_count, 0)

    def test_edb_wrong_id_or_missing_publishdate_fails_closed(self):
        cases = (
            FakeChoiceClient(metadata=metadata_result(code="EMM00000001")),
            FakeChoiceClient(edb=edb_result(indicators=["VALUE", "REVISION"])),
        )
        for client in cases:
            with self.subTest(client=client), tempfile.TemporaryDirectory() as directory:
                evidence = self.service(directory, client).fetch_edb_publish_dates(
                    ["EMM00087117"]
                )
                self.assertEqual(evidence.status, "failed")
                self.assertEqual(evidence.record_count, 0)

    def test_raw_and_normalized_record_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, FakeChoiceClient())
            evidence = service.fetch_sw2021_classification("000333.SZ")
            storage = ChoiceCandidateStorage(directory)
            path = storage.evidence_path(evidence)
            raw_path = storage.raw_path(evidence.raw_content_sha256)
            original_raw = raw_path.read_bytes()
            raw_path.write_bytes(original_raw + b" ")
            with self.assertRaisesRegex(ChoiceCandidateError, "canonical JSON|hash"):
                storage.read(path)
            raw_path.write_bytes(original_raw)

            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["records"][0]["classification_raw"] = "tampered"
            path.write_bytes(canonical_json_bytes(payload))
            with self.assertRaisesRegex(ChoiceCandidateError, "evidence_id mismatch"):
                storage.read(path)

    def test_evidence_cannot_be_relabelled_formal(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, FakeChoiceClient())
            evidence = service.fetch_sw2021_classification("000333.SZ")
            payload = evidence.to_dict()
            payload["formal_truth_eligible"] = True
            with self.assertRaisesRegex(ChoiceCandidateError, "never be formal truth"):
                ChoiceCandidateEvidence.from_dict(payload)

    def test_request_contract_rejects_wrong_indicator_options_and_arbitrary_dispatch(self):
        request = sw2021_request("000333.SZ")
        raw = copy.deepcopy(request)
        raw["sdk_calls"][0]["args"][1] = "TOTALSHARE"
        with self.assertRaisesRegex(ChoiceCandidateError, "non-fixed"):
            replay_candidate_raw(
                "sw2021_classification",
                raw,
                canonical_json_bytes({}),
            )

        raw = copy.deepcopy(edb_publish_dates_request(["EMM00087117"]))
        raw["sdk_calls"][1]["args"][1] = raw["sdk_calls"][1]["args"][1].replace(
            "IsPublishDate=1", "IsPublishDate=0"
        )
        with self.assertRaisesRegex(ChoiceCandidateError, "non-fixed"):
            replay_candidate_raw("edb_publish_dates", raw, canonical_json_bytes({}))

        with self.assertRaisesRegex(ProviderQueryError, "read-only allowlist"):
            ChoiceProvider._sdk_call(FakeChoiceClient(), "porder")

    def test_partial_interface_failure_does_not_relabel_other_interfaces(self):
        client = FakeChoiceClient(sector=result(10001012, "insufficient user access"))
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, client)
            css = service.fetch_sw2021_classification("000333.SZ")
            sector = service.fetch_historical_sector_membership("001004", "2024-06-30")
            self.assertEqual(css.status, "passed")
            self.assertEqual(sector.status, "not_configured")
            self.assertNotEqual(css.evidence_id, sector.evidence_id)

    def test_cli_requires_explicit_mode_and_offline_does_not_load_sdk(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["sw2021", "--instrument", "000333.SZ"])
        with tempfile.TemporaryDirectory() as directory:
            self.service(directory, FakeChoiceClient()).fetch_sw2021_classification(
                "000333.SZ"
            )
            args = parser.parse_args(
                [
                    "--mode",
                    "offline",
                    "--storage-root",
                    directory,
                    "sw2021",
                    "--instrument",
                    "000333.SZ",
                ]
            )
            service = ChoiceCandidateService(
                directory,
                provider=ChoiceProvider(
                    sdk_loader=lambda: (_ for _ in ()).throw(
                        AssertionError("offline replay must not load Choice SDK")
                    )
                ),
            )
            result_payload = run_probe(service, args)
            self.assertEqual(result_payload["status"], "passed")
            self.assertEqual(result_payload["account_status"], "not_assessed")
            self.assertFalse(result_payload["formal_truth_eligible"])


if __name__ == "__main__":
    unittest.main()
