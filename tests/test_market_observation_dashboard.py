import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent.market_observation_dashboard import (
    ObservationValidationError,
    render_dashboard,
    validate_observation,
    write_dashboard,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schemas" / "market_observation.v0.1.json"
SCHEMA_HASH = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


def draft_observation_fixture(as_of: str = "2026-08-05") -> dict:
    market_as_of = f"{as_of}T15:00:00+08:00"
    return {
        "schema_version": "market-observation-v0.1",
        "observation_id": f"cn-market-{as_of}-close",
        "status": "diagnostic_only_not_admitted",
        "as_of": as_of,
        "market_as_of": market_as_of,
        "decision_time": f"{as_of}T15:10:00+08:00",
        "generated_at": f"{as_of}T18:00:00+08:00",
        "market": "CN",
        "horizons_trading_days": [20, 60],
        "purpose": "建立三层市场观察基线，不形成订单或实盘信号",
        "overall": {
            "macro_environment": "neutral_defensive",
            "market_state": "short_term_broad_repair_medium_term_unconfirmed",
            "risk_budget_observation": "low_to_medium",
            "research_action": "observe_only",
            "trade_action": None,
        },
        "macro": {
            "state": "neutral_defensive",
            "liquidity": "accommodative",
            "credit_transmission": "weak",
            "growth_momentum": "weakening",
            "inflation_pressure": "contained",
            "currency_pressure": "low",
            "equity_risk_appetite": "short_term_repair_medium_term_unconfirmed",
            "observations": [
                {
                    "metric": "manufacturing_pmi",
                    "period": "2026-07",
                    "available_at": "2026-07-31T09:30:00+08:00",
                    "value": 49.2,
                    "unit": "percent",
                    "source": "https://example.test/pmi",
                },
                {
                    "metric": "csi300_close",
                    "period": as_of,
                    "available_at": market_as_of,
                    "value": 4658.15,
                    "return_1d_pct": 1.24,
                    "return_20d_pct": -2.05,
                    "return_60d_pct": -5.86,
                    "source": "https://example.test/csi300",
                },
            ],
            "unknowns": ["7月金融数据尚未公布"],
            "invalidation": ["连续两个月PMI回到50以上则上调"],
        },
        "industry": {
            "classification": "CSI_LEVEL_1_current_11_sector",
            "classification_as_of": as_of,
            "available_at": market_as_of,
            "benchmark_id": "000985",
            "benchmark_name": "中证全指",
            "benchmark_return_1d_pct": 0.0,
            "benchmark_return_20d_pct": -4.47,
            "benchmark_return_60d_pct": -10.96,
            "benchmark_turnover_cny_100m": 25729.06,
            "benchmark_turnover_ratio_vs_prior_20d": 1.06,
            "cross_section": {
                "up_1d_count": 1,
                "above_prior_ma20_count": 1,
                "above_prior_ma60_count": 0,
                "outperform_20d_count": 1,
                "outperform_60d_count": 1,
                "sector_count": 1,
                "constituent_breadth_available": False,
            },
            "sectors": [
                {
                    "code": "932082",
                    "name": "医药卫生",
                    "state": "leading",
                    "return_1d_pct": 0.8,
                    "return_20d_pct": 6.36,
                    "excess_20d_pct": 11.34,
                    "return_60d_pct": None,
                    "excess_60d_pct": 8.28,
                    "above_prior_ma20": True,
                    "above_prior_ma60": False,
                    "turnover_ratio": 0.95,
                }
            ],
            "source_template": "https://example.test/index?code={index_code}",
            "methodology_source": "https://example.test/methodology.pdf",
            "limitations": ["成分股广度尚未取得"],
        },
        "stock": {
            "available_at": market_as_of,
            "focus": {
                "stock_id": "000333.SZ",
                "name": "美的集团",
                "state": "relative_strength_fundamentals_unconfirmed",
                "close": 85.47,
                "price_available_at": market_as_of,
                "return_20d_pct": 7.63,
                "return_60d_pct": 11.92,
                "excess_vs_csi300_20d_pct_points": 9.68,
                "excess_vs_csi300_60d_pct_points": 17.78,
                "excess_vs_home_appliance_etf_20d_pct_points": 7.21,
                "excess_vs_home_appliance_etf_60d_pct_points": 20.52,
                "latest_close_drawdown_from_20d_high_pct": -3.91,
                "fundamental_as_of": "2026Q1",
                "fundamental_available_at": "2026-04-30",
                "revenue_yoy_pct": 2.55,
                "net_profit_parent_yoy_pct": 2.03,
                "net_profit_ex_items_yoy_pct": -14.02,
                "operating_cash_flow_yoy_pct": 1.45,
                "supporting_evidence": ["收入仍为正增长"],
                "counter_evidence": ["扣非利润下降"],
                "price_source": "https://example.test/midea-price",
                "fundamental_source": "https://example.test/midea.pdf",
            },
            "peer_prices": [
                {
                    "stock_id": "600690.SH",
                    "name": "海尔智家",
                    "close": 22.34,
                    "price_available_at": market_as_of,
                    "return_20d_pct": 10.10,
                    "return_60d_pct": 5.28,
                }
            ],
            "cross_industry_observation_samples": [
                {
                    "industry": "信息技术",
                    "stock_id": "688981.SH",
                    "name": "中芯国际",
                    "state": "volume_fundamentals_strong_margin_and_price_weak",
                    "close": 125.45,
                    "price_available_at": market_as_of,
                    "return_20d_pct": -17.52,
                    "industry_ex_stock_return_20d_pct_approx": -16.49,
                    "excess_20d_pct_points_approx": -1.04,
                    "return_60d_pct": 2.84,
                    "industry_return_60d_including_stock_pct": -6.17,
                    "excess_60d_pct_points_approx": 9.01,
                    "support": "收入增长",
                    "counter_evidence": "营业利润下降",
                    "watch_reason": "验证基本面与价格冲突",
                    "invalidation": "收入低于指引则失效",
                    "fundamental_available_at": "2026-05-14",
                    "fundamental_source": "https://example.test/smic.pdf",
                }
            ],
            "limitations": ["60日不是精确leave-one-out"],
        },
        "three_layer_conflicts": ["信息技术基本面与价格冲突"],
        "deep_read_queue": [
            {"priority": 1, "subject": "信息技术", "reason": "可能改变落后判断"}
        ],
        "data_quality": {
            "official_industry_source": "success",
            "official_macro_sources": "partial_success",
            "tencent_market_history": "success",
            "eastmoney_market_history": "failed_after_3_attempts_not_used",
            "eastmoney_industry_board": "partial_snapshot_then_pagination_failure_not_used",
            "point_in_time_constituent_breadth": "missing",
            "official_trade_calendar_adapter": "not_admitted",
            "formal_factor_eligibility": False,
        },
    }


def sealed_observation_fixture(as_of: str = "2026-08-05") -> dict:
    payload = draft_observation_fixture(as_of)
    payload["comparison"] = {
        "status": "first_baseline",
        "previous_observation_id": None,
        "previous_as_of": None,
        "previous_sha256": None,
        "overall_state_changes": [],
        "industry_state_changes": [],
        "stock_state_changes": [],
        "new_conflicts": [],
        "resolved_conflicts": [],
        "has_material_change": False,
    }
    payload["pipeline"] = {
        "producer": "market-observation-pipeline-v0.1",
        "standard_cli_generated": True,
        "sealed_at": f"{as_of}T18:05:00+08:00",
        "schema_path": "schemas/market_observation.v0.1.json",
        "schema_sha256": SCHEMA_HASH,
        "draft_sha256": "b" * 64,
    }
    return payload


def encoded_json(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def standard_manifest(input_path: Path, payload: dict, source_hash: str) -> dict:
    pipeline = payload["pipeline"]
    comparison = payload["comparison"]
    return {
        "manifest_version": "market-observation-manifest-v0.2",
        "observation_id": payload["observation_id"],
        "status": payload["status"],
        "as_of": payload["as_of"],
        "generated_at": payload["generated_at"],
        "sealed_at": pipeline["sealed_at"],
        "producer": pipeline["producer"],
        "standard_cli_generated": True,
        "repository_commit": None,
        "working_tree_dirty_at_generation": None,
        "schema": {
            "path": pipeline["schema_path"],
            "sha256": pipeline["schema_sha256"],
            "schema_version": payload["schema_version"],
        },
        "inputs": [
            {
                "role": "draft_observation",
                "path": "data/inbox/market-observation-draft.json",
                "sha256": pipeline["draft_sha256"],
            },
            {
                "role": "schema",
                "path": pipeline["schema_path"],
                "sha256": pipeline["schema_sha256"],
            },
        ],
        "outputs": [
            {
                "role": "sealed_observation",
                "path": input_path.resolve().as_posix(),
                "sha256": source_hash,
            }
        ],
        "source_status": copy.deepcopy(payload["data_quality"]),
        "comparison": {
            "status": comparison["status"],
            "previous_observation_id": comparison["previous_observation_id"],
            "previous_sha256": comparison["previous_sha256"],
        },
        "admission": {
            "source_data_admitted": False,
            "objective_factor_admitted": False,
            "research_report_factor_admitted": False,
            "paper_strategy_admitted": False,
            "live_trading_allowed": False,
        },
        "aliases": [],
    }


class MarketObservationDashboardTest(unittest.TestCase):
    def test_write_dashboard_verifies_standard_manifest_and_first_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "cn-market-2026-08-05-close.sealed.json"
            raw = encoded_json(sealed_observation_fixture())
            input_path.write_bytes(raw)
            source_hash = hashlib.sha256(raw).hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(standard_manifest(input_path, sealed_observation_fixture(), source_hash)),
                encoding="utf-8",
            )
            first = root / "first.html"
            second = root / "second.html"

            write_dashboard(input_path, first, manifest_path)
            write_dashboard(input_path, second, manifest_path)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            content = first.read_text(encoding="utf-8")
            self.assertIn("Legacy 历史行情诊断（补充源）", content)
            self.assertIn("当前连接失败 · 已隔离未使用", content)
            self.assertIn("行情截至 2026-08-05T15:00:00+08:00 · 决策时点 2026-08-05T15:10:00+08:00", content)
            self.assertIn("const marketTime = new Date", content)
            self.assertIn("行情距今", content)
            self.assertIn("决策更新", content)
            self.assertIn("建立三层市场观察基线，不形成订单或实盘信号", content)
            self.assertIn("文件完整性已核验 · 来源仍未准入", content)
            self.assertIn("首次基线 · 暂无前次变化", content)
            self.assertIn("首次可比基线", content)
            self.assertIn("未生成交易动作", content)
            self.assertIn("医药卫生", content)
            self.assertIn("中芯国际", content)
            self.assertIn("0.00%", content)
            self.assertIn('metric-60d missing">—', content)
            self.assertIn("≈ -1.04 个百分点", content)
            self.assertNotIn("None", content)
            self.assertFalse(first.with_suffix(".html.tmp").exists())

    def test_render_escapes_text_and_rejects_unsafe_links(self):
        payload = sealed_observation_fixture()
        payload["stock"]["focus"]["name"] = "</script><script>alert(1)</script>"
        payload["stock"]["focus"]["fundamental_source"] = "javascript:alert(2)"

        content = render_dashboard(payload, "a" * 64)

        self.assertIn("&lt;/script&gt;&lt;script&gt;alert(1)&lt;/script&gt;", content)
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertNotIn("javascript:alert", content)
        self.assertNotIn("<script src=", content)
        self.assertNotIn("<link rel=", content)
        self.assertNotIn("买入", content)
        self.assertNotIn("卖出", content)
        self.assertNotIn("下单", content)

    def test_rejects_future_and_same_day_date_only_evidence(self):
        cases = [
            "2026-08-06",
            "2026-08-05",
        ]
        for available_at in cases:
            with self.subTest(available_at=available_at):
                payload = draft_observation_fixture()
                payload["macro"]["observations"][0]["available_at"] = available_at
                with self.assertRaises(ObservationValidationError) as raised:
                    validate_observation(payload)
                self.assertIn("availability", str(raised.exception))

    def test_trade_action_and_factor_eligibility_fail_closed(self):
        cases = [
            (lambda item: item["overall"].__setitem__("trade_action", "buy"), "trade_action"),
            (lambda item: item["data_quality"].__setitem__("formal_factor_eligibility", True), "formal_factor_eligibility"),
        ]
        for mutate, message in cases:
            with self.subTest(message=message):
                payload = draft_observation_fixture()
                mutate(payload)
                with self.assertRaisesRegex(ObservationValidationError, message):
                    validate_observation(payload)

    def test_macro_state_must_equal_overall_macro_environment(self):
        payload = draft_observation_fixture()
        payload["macro"]["state"] = "risk_on"

        with self.assertRaisesRegex(ObservationValidationError, "macro.state"):
            validate_observation(payload)

    def test_requires_strict_fields_status_schema_and_timezone(self):
        cases = [
            ("market_as_of", None, "missing required field"),
            ("status", "research_only_not_trade_eligible", "unsupported observation status"),
            ("schema_version", "market-observation-v9", "unsupported schema_version"),
            ("decision_time", "2026-08-05T15:10:00", "timezone offset"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                payload = draft_observation_fixture()
                if value is None:
                    payload.pop(field)
                else:
                    payload[field] = value
                with self.assertRaisesRegex(ObservationValidationError, message):
                    validate_observation(payload)

    def test_manifest_binds_full_path_role_schema_and_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "cn-market-2026-08-05-close.sealed.json"
            payload = sealed_observation_fixture()
            raw = encoded_json(payload)
            input_path.write_bytes(raw)
            source_hash = hashlib.sha256(raw).hexdigest()
            base = standard_manifest(input_path, payload, source_hash)

            cases = [
                (
                    "full_path",
                    lambda item: item["outputs"][0].__setitem__("path", (root / "other" / input_path.name).as_posix()),
                    "current SHA-256",
                ),
                (
                    "role",
                    lambda item: item["outputs"][0].__setitem__("role", "draft_observation"),
                    "sealed_observation output",
                ),
                (
                    "schema",
                    lambda item: item["schema"].__setitem__("schema_version", "market-observation-v9"),
                    None,
                ),
                (
                    "schema_path",
                    lambda item: item["schema"].__setitem__("path", "schemas/other.json"),
                    None,
                ),
                (
                    "schema_hash",
                    lambda item: item["schema"].__setitem__("sha256", "d" * 64),
                    None,
                ),
                (
                    "hash",
                    lambda item: item["outputs"][0].__setitem__("sha256", "0" * 64),
                    None,
                ),
                (
                    "standard_cli",
                    lambda item: item.__setitem__("standard_cli_generated", False),
                    None,
                ),
                (
                    "admission",
                    lambda item: item["admission"].__setitem__("source_data_admitted", True),
                    None,
                ),
                (
                    "source_status",
                    lambda item: item["source_status"].__setitem__("official_industry_source", "forged"),
                    None,
                ),
                (
                    "comparison",
                    lambda item: item["comparison"].__setitem__("status", "compared"),
                    None,
                ),
                (
                    "sealed_at",
                    lambda item: item.__setitem__("sealed_at", "2026-08-05T18:06:00+08:00"),
                    None,
                ),
            ]
            for name, mutate, message in cases:
                with self.subTest(name=name):
                    manifest = copy.deepcopy(base)
                    mutate(manifest)
                    manifest_path = root / f"{name}.manifest.json"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    if message:
                        context = self.assertRaisesRegex(ObservationValidationError, message)
                    else:
                        context = self.assertRaises(ObservationValidationError)
                    with context:
                        write_dashboard(input_path, root / f"{name}.html", manifest_path)

    def test_unsealed_draft_cannot_be_blessed_by_a_forged_standard_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "draft.json"
            draft = draft_observation_fixture()
            raw = encoded_json(draft)
            input_path.write_bytes(raw)
            forged_manifest = standard_manifest(
                input_path,
                sealed_observation_fixture(),
                hashlib.sha256(raw).hexdigest(),
            )
            manifest_path = root / "forged.manifest.json"
            manifest_path.write_text(json.dumps(forged_manifest), encoding="utf-8")

            with self.assertRaises(ObservationValidationError):
                write_dashboard(input_path, root / "forged.html", manifest_path)
            self.assertFalse((root / "forged.html").exists())

    def test_dashboard_snapshot_refuses_non_identical_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "cn-market-2026-08-05-close.sealed.json"
            payload = sealed_observation_fixture()
            raw = encoded_json(payload)
            input_path.write_bytes(raw)
            source_hash = hashlib.sha256(raw).hexdigest()
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(standard_manifest(input_path, payload, source_hash)), encoding="utf-8")
            output_path = root / "snapshot.html"
            output_path.write_text("occupied", encoding="utf-8")

            with self.assertRaisesRegex(ObservationValidationError, "refusing to overwrite non-identical dashboard"):
                write_dashboard(input_path, output_path, manifest_path, allow_replace=False)


if __name__ == "__main__":
    unittest.main()
