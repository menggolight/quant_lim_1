"""Deterministic, high-precision claim extraction.

The V1 extractor intentionally has low recall.  A sentence must identify a
target, a forecast period and either a numeric value or an explicit direction.
Conditional, risk-only and vague long-term commentary is excluded instead of
being retroactively interpreted as a successful forecast.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Mapping

from .models import ResearchClaim, ResearchReport, decimal_or_none, stable_identifier


EXTRACTOR_VERSION = "rule-v1.2.0"


def extractor_bundle_sha256() -> str:
    """Hash the exact executable rules/lexicons used for this extraction."""

    return sha256(Path(__file__).read_bytes()).hexdigest()


POSITIVE_RATINGS = frozenset(
    {"买入", "强烈推荐", "推荐", "增持", "看好", "优于大市", "跑赢大市", "outperform", "buy"}
)
NEUTRAL_RATINGS = frozenset(
    {"中性", "持有", "同步大市", "与大市同步", "neutral", "hold"}
)
NEGATIVE_RATINGS = frozenset(
    {"减持", "卖出", "回避", "看淡", "弱于大市", "跑输大市", "underperform", "sell"}
)

# ``ratingChange`` is a source enum, not free text.  Keep the accepted set
# deliberately small: unknown values and first coverage do not prove a change
# and therefore remain available only in the report metadata for audit.
RATING_CHANGE_DIRECTIONS = {
    "上调": 1,
    "调高": 1,
    "upgrade": 1,
    "upgraded": 1,
    "维持": 0,
    "不变": 0,
    "maintain": 0,
    "maintained": 0,
    "unchanged": 0,
    "下调": -1,
    "调低": -1,
    "downgrade": -1,
    "downgraded": -1,
}


TARGET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EPS", re.compile(r"(?:EPS|基本每股收益|每股收益)", re.IGNORECASE)),
    ("net_profit", re.compile(r"(?:归母净利润|扣非净利润|净利润)")),
    ("revenue", re.compile(r"(?:营业总收入|营业收入|营收)")),
    ("gross_margin", re.compile(r"(?:毛利率|净利率)")),
    ("earnings_revision", re.compile(r"(?:盈利预测|盈利预期|业绩预期|利润预测)")),
    ("target_price", re.compile(r"(?:目标价|目标价格)")),
    ("GDP", re.compile(r"(?:GDP|国内生产总值)", re.IGNORECASE)),
    ("CPI", re.compile(r"(?:CPI|居民消费价格)", re.IGNORECASE)),
    ("PPI", re.compile(r"(?:PPI|工业生产者出厂价格)", re.IGNORECASE)),
    ("TSF", re.compile(r"(?:社融|社会融资规模)")),
    ("M2", re.compile(r"(?:M2|广义货币)", re.IGNORECASE)),
    ("bond_yield", re.compile(r"(?:国债收益率|国债利率)")),
    ("USD_CNY", re.compile(r"(?:美元兑人民币|人民币兑美元|USD\s*/\s*CNY|人民币汇率)", re.IGNORECASE)),
    ("interest_rate", re.compile(r"(?:LPR|MLF|政策利率|存款利率|贷款利率)", re.IGNORECASE)),
    ("liquidity", re.compile(r"(?:流动性|资金面)")),
    ("commodity", re.compile(r"(?:原油|油价|黄金|金价|铜价|商品价格|商品指数)")),
    ("market_direction", re.compile(r"(?:沪深\s*300|上证指数|创业板指|A股|大盘|权益市场)", re.IGNORECASE)),
    ("industry_demand", re.compile(r"(?:需求|销量|销售量|出货量|订单)")),
    ("industry_price", re.compile(r"(?:产品价格|均价|价格中枢|售价)")),
    ("industry_inventory", re.compile(r"(?:库存|库销比)")),
    ("industry_capacity", re.compile(r"(?:产能|产量|开工率|利用率)")),
    ("industry_profit", re.compile(r"(?:利润|盈利|毛利率|净利率)")),
    ("industry_cycle", re.compile(r"(?:景气度|景气指数)")),
)

DIMENSION_TARGETS = {
    "macro": frozenset(
        {
            "GDP",
            "CPI",
            "PPI",
            "TSF",
            "M2",
            "bond_yield",
            "USD_CNY",
            "interest_rate",
            "liquidity",
            "commodity",
            "market_direction",
        }
    ),
    "industry": frozenset(
        {
            "industry_demand",
            "industry_price",
            "industry_inventory",
            "industry_capacity",
            "industry_profit",
            "industry_cycle",
        }
    ),
    "stock": frozenset(
        {"EPS", "net_profit", "revenue", "gross_margin", "earnings_revision", "target_price"}
    ),
}

FORWARD_PATTERN = re.compile(
    r"(?:预计|预测|预期|有望|将(?:会)?|目标|展望|判断|维持|或将|料将|上调|下调|上修|下修)"
)
CONDITIONAL_PATTERN = re.compile(r"(?:如果|若|一旦|除非|假设|情景下|取决于|前提是|风险提示|风险因素)")
VAGUE_PATTERN = re.compile(r"(?:长期向好|长期看好|政策支持|值得关注|建议关注|持续关注|未来可期)")
POSITIVE_PATTERN = re.compile(
    r"(?:上升|上涨|增长|增加|扩大|改善|回升|走高|走强|宽松|领先|跑赢|看多|上调|调高|上修)"
)
NEGATIVE_PATTERN = re.compile(
    r"(?:下降|下跌|减少|收缩|恶化|回落|走低|走弱|紧缩|落后|跑输|看空|下调|调低|下修)"
)
FLAT_PATTERN = re.compile(r"(?:持平|稳定|不变|震荡|中性)")
BENCHMARK_PATTERN = re.compile(r"(?:同比|环比|较上年|较去年|较前值|相对沪深\s*300|相对行业)", re.IGNORECASE)

ABSOLUTE_PERIOD_PATTERN = re.compile(
    r"(?:(?:20\d{2})年(?:第?[一二三四1234]季度|Q[1-4]|[上下]半年|\d{1,2}月|度)?|"
    r"(?:20\d{2})Q[1-4]|(?:本月|下月|本季度|下季度|年内|年底|年末|明年|后年)|"
    r"(?:一|二|三|四|1|2|3|4)季度)",
    re.IGNORECASE,
)
RELATIVE_PERIOD_PATTERN = re.compile(
    r"(?:未来|随后|接下来)\s*(?P<count>\d{1,3}|[一二三四五六七八九十]+)\s*个?"
    r"(?P<unit>交易日|日|周|月|季度|年)"
)

NUMBER = r"[+-]?\d+(?:\.\d+)?"
NUMERIC_PATTERN = re.compile(
    rf"(?P<a>{NUMBER})\s*(?P<unit>%|％|个百分点|个基点|基点|bp|BP|万亿元|亿元|万元|元|点|倍)"
    rf"(?:\s*(?:-|—|~|～|至|到)\s*(?P<b>{NUMBER})\s*(?P<unit_b>%|％|个百分点|个基点|基点|bp|BP|万亿元|亿元|万元|元|点|倍)?)?"
)


def _rating_direction(rating: str) -> int | None:
    normalised = rating.strip().lower()
    if normalised in POSITIVE_RATINGS:
        return 1
    if normalised in NEUTRAL_RATINGS:
        return 0
    if normalised in NEGATIVE_RATINGS:
        return -1
    return None


def _rating_change_direction(rating_change: str) -> int | None:
    """Return only a deterministic, explicitly enumerated rating change."""

    normalised = " ".join(str(rating_change or "").strip().lower().split())
    return RATING_CHANGE_DIRECTIONS.get(normalised)


def _direction(sentence: str) -> int:
    positive = bool(POSITIVE_PATTERN.search(sentence))
    negative = bool(NEGATIVE_PATTERN.search(sentence))
    if positive == negative:
        return 0
    return 1 if positive else -1


def _chinese_integer(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return digits[value]


def _period_and_horizon(sentence: str, dimension: str, published: date) -> tuple[str, int] | None:
    relative = RELATIVE_PERIOD_PATTERN.search(sentence)
    if relative:
        count = _chinese_integer(relative.group("count"))
        unit = relative.group("unit")
        multipliers = {"交易日": 1, "日": 5 / 7, "周": 5, "月": 21, "季度": 63, "年": 252}
        return relative.group(0), max(1, round(count * multipliers[unit]))
    absolute = ABSOLUTE_PERIOD_PATTERN.search(sentence)
    if not absolute:
        return None
    period = absolute.group(0)
    if "月" in period:
        horizon = 21
    elif "季度" in period or re.search(r"Q[1-4]", period, re.IGNORECASE):
        horizon = 63
    elif "半年" in period:
        horizon = 126
    elif period in ("年内", "年底", "年末"):
        horizon = _weekday_count(published, date(published.year, 12, 31))
    elif period == "明年":
        horizon = _weekday_count(published, date(published.year + 1, 12, 31))
    elif period == "后年":
        horizon = _weekday_count(published, date(published.year + 2, 12, 31))
    elif re.fullmatch(r"20\d{2}年(?:度)?", period):
        target_year = int(period[:4])
        horizon = _weekday_count(published, date(target_year, 12, 31))
    else:
        horizon = 60 if dimension == "macro" else 120
    return period, horizon


def _numeric(sentence: str) -> tuple[Decimal | None, Decimal | None, str]:
    match = NUMERIC_PATTERN.search(sentence)
    if not match:
        return None, None, ""
    first = Decimal(match.group("a"))
    second = Decimal(match.group("b")) if match.group("b") else first
    low, high = sorted((first, second))
    unit = match.group("unit")
    if unit == "％":
        unit = "%"
    if unit.lower() == "bp":
        unit = "bp"
    return low, high, unit


def _metadata_decimal(value: object) -> Decimal | None:
    if value is None or str(value).strip().lower() in {"", "-", "--", "null", "none", "nan"}:
        return None
    return decimal_or_none(value)


def _target(sentence: str, dimension: str) -> str | None:
    allowed = DIMENSION_TARGETS[dimension]
    for target_type, pattern in TARGET_PATTERNS:
        if target_type in allowed and pattern.search(sentence):
            return target_type
    return None


def _weekday_count(start: date, end: date) -> int:
    """Approximate trading days with weekdays, intentionally excluding no guessed holidays."""

    if end < start:
        return 1
    total_days = (end - start).days + 1
    full_weeks, remainder = divmod(total_days, 7)
    weekdays = full_weeks * 5
    for offset in range(remainder):
        if (start.weekday() + offset) % 7 < 5:
            weekdays += 1
    return max(1, weekdays)


def _sentences(text: str) -> Iterable[str]:
    for chunk in re.split(r"(?<=[。！？!?；;])|[\r\n]+", text):
        sentence = re.sub(r"\s+", " ", chunk).strip(" \t。；;")
        if 8 <= len(sentence) <= 500:
            yield sentence


@dataclass(frozen=True)
class _Candidate:
    target_type: str
    direction: int
    value_min: Decimal | None
    value_max: Decimal | None
    unit: str
    benchmark: str
    forecast_period: str
    horizon_days: int
    evidence: str
    confidence: float
    evidence_source_kind: str = "structured/source_record"


class RuleBasedExtractor:
    """High-precision, zero-token extractor for structured and textual claims."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.95,
        extractor_version: str = EXTRACTOR_VERSION,
        parser_version: str = "",
        prompt_version: str = "none",
    ) -> None:
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be in [0, 1]")
        self.min_confidence = float(min_confidence)
        self.extractor_version = extractor_version
        self.parser_version = str(parser_version or "").strip()
        self.prompt_version = str(prompt_version or "").strip()

    def extract(
        self,
        report: ResearchReport,
        text: str | None = None,
    ) -> tuple[ResearchClaim, ...]:
        candidates = [
            *self._structured(report),
            *self._textual(
                report,
                text,
                evidence_source_kind=(
                    "textual/pdf" if text is not None else "structured/source_record"
                ),
            ),
        ]
        # One claim per report/topic/period/horizon.  Keep the strongest fully
        # deterministic candidate rather than counting repeated prose.
        selected: dict[tuple[str, str, int], _Candidate] = {}
        for candidate in candidates:
            if candidate.confidence < self.min_confidence:
                continue
            key = (candidate.target_type, candidate.forecast_period, candidate.horizon_days)
            existing = selected.get(key)
            if existing is None or (candidate.confidence, candidate.evidence) > (
                existing.confidence,
                existing.evidence,
            ):
                selected[key] = candidate
        claims = [self._to_claim(report, candidate) for candidate in selected.values()]
        claims.sort(key=lambda item: (item.target_type, item.forecast_period, item.claim_id))
        return tuple(claims)

    def _to_claim(self, report: ResearchReport, candidate: _Candidate) -> ResearchClaim:
        bundle_hash = extractor_bundle_sha256()
        evidence_source_hash = (
            report.pdf_sha256
            if candidate.evidence_source_kind == "textual/pdf"
            else report.content_hash
        )
        evidence_parser_version = (
            self.parser_version
            if candidate.evidence_source_kind == "textual/pdf"
            else "source-record-v1"
        )
        evidence_prompt_version = (
            self.prompt_version
            if candidate.evidence_source_kind == "textual/pdf"
            else "none"
        )
        claim_id = stable_identifier(
            "claim",
            self.extractor_version,
            bundle_hash,
            report.report_id,
            candidate.evidence_source_kind,
            evidence_source_hash,
            evidence_parser_version,
            evidence_prompt_version,
            report.dimension,
            report.subject_id,
            candidate.target_type,
            candidate.direction,
            candidate.value_min,
            candidate.value_max,
            candidate.unit,
            candidate.benchmark,
            candidate.forecast_period,
            candidate.horizon_days,
            report.available_at.isoformat(),
            candidate.evidence,
            candidate.confidence,
        )
        return ResearchClaim(
            claim_id=claim_id,
            report_id=report.report_id,
            dimension=report.dimension,
            subject_id=report.subject_id,
            target_type=candidate.target_type,
            direction=candidate.direction,
            value_min=candidate.value_min,
            value_max=candidate.value_max,
            unit=candidate.unit,
            benchmark=candidate.benchmark,
            forecast_period=candidate.forecast_period,
            horizon_days=candidate.horizon_days,
            available_at=report.available_at,
            evidence_span=candidate.evidence,
            extractor_version=self.extractor_version,
            extraction_confidence=candidate.confidence,
            evidence_source_kind=candidate.evidence_source_kind,
            evidence_source_hash=evidence_source_hash,
            evidence_parser_version=evidence_parser_version,
            evidence_prompt_version=evidence_prompt_version,
            extractor_bundle_sha256=bundle_hash,
        )

    def _structured(self, report: ResearchReport) -> Iterable[_Candidate]:
        direction = _rating_direction(report.rating)
        horizon = 60 if report.dimension == "macro" else 120
        if direction is not None and report.dimension in ("industry", "stock"):
            yield _Candidate(
                target_type=f"{report.dimension}_rating",
                direction=direction,
                value_min=Decimal("0") if direction == 0 else None,
                value_max=Decimal("0") if direction == 0 else None,
                unit="rating",
                benchmark="",
                forecast_period=f"{horizon}TD",
                horizon_days=horizon,
                evidence=f"结构化评级：{report.rating}",
                confidence=0.995,
            )

        rating_change_direction = _rating_change_direction(report.rating_change)
        if report.dimension == "stock" and rating_change_direction is not None:
            yield _Candidate(
                target_type="rating_change",
                direction=rating_change_direction,
                # ResearchClaim deliberately distinguishes an explicit
                # maintained rating (numeric zero) from a missing/unknown
                # change, which produces no claim at all.
                value_min=(
                    Decimal("0") if rating_change_direction == 0 else None
                ),
                value_max=(
                    Decimal("0") if rating_change_direction == 0 else None
                ),
                unit="rating_change",
                benchmark="",
                forecast_period="120TD",
                horizon_days=120,
                evidence=f"结构化评级变化：{report.rating_change}",
                confidence=0.995,
            )

        if report.dimension == "stock" and (
            report.target_price_min is not None or report.target_price_max is not None
        ):
            yield _Candidate(
                target_type="target_price",
                direction=0,
                value_min=report.target_price_min,
                value_max=report.target_price_max,
                unit="CNY",
                benchmark="",
                forecast_period="120TD",
                horizon_days=120,
                evidence=(
                    f"结构化目标价：{report.target_price_min or ''}"
                    f"-{report.target_price_max or ''}元"
                ),
                confidence=0.995,
            )

        if report.dimension != "stock":
            return
        year = report.published_at.astimezone(report.available_at.tzinfo).year
        fields = (
            ("predictThisYearEps", year),
            ("predictNextYearEps", year + 1),
            ("predictNextTwoYearEps", year + 2),
        )
        for field, forecast_year in fields:
            value = _metadata_decimal(report.metadata.get(field))
            if value is None:
                continue
            yield _Candidate(
                target_type="EPS",
                direction=0,
                value_min=value,
                value_max=value,
                unit="CNY/share",
                benchmark="annual_report_basic_eps",
                forecast_period=f"{forecast_year}FY",
                horizon_days=_weekday_count(
                    report.available_at.date(), date(forecast_year, 12, 31)
                ),
                evidence=f"结构化{forecast_year}年EPS预测：{value}",
                confidence=0.995,
            )

    def _textual(
        self,
        report: ResearchReport,
        text: str | None,
        *,
        evidence_source_kind: str,
    ) -> Iterable[_Candidate]:
        # Keep provenance unambiguous: title/list-record prose is record
        # evidence, while an explicitly supplied body is PDF evidence.
        source_text = report.title if text is None else text
        for sentence in _sentences(source_text):
            if CONDITIONAL_PATTERN.search(sentence):
                continue
            target_type = _target(sentence, report.dimension)
            period = _period_and_horizon(
                sentence, report.dimension, report.published_at.date()
            )
            if target_type is None or period is None or not FORWARD_PATTERN.search(sentence):
                continue
            low, high, unit = _numeric(sentence)
            direction = _direction(sentence)
            if low is None and direction == 0:
                # This is the core "空泛观点不评分" gate.
                continue
            if low is None and VAGUE_PATTERN.search(sentence):
                continue
            benchmark_match = BENCHMARK_PATTERN.search(sentence)
            confidence = 0.97 if low is not None else 0.95
            yield _Candidate(
                target_type=target_type,
                direction=direction,
                value_min=low,
                value_max=high,
                unit=unit,
                benchmark=benchmark_match.group(0) if benchmark_match else "",
                forecast_period=period[0],
                horizon_days=period[1],
                evidence=sentence,
                confidence=confidence,
                evidence_source_kind=evidence_source_kind,
            )


def extract_claims(
    report: ResearchReport,
    text: str | None = None,
    *,
    min_confidence: float = 0.95,
) -> tuple[ResearchClaim, ...]:
    """Convenience wrapper around :class:`RuleBasedExtractor`."""

    return RuleBasedExtractor(min_confidence=min_confidence).extract(report, text)
