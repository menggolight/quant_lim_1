"""SQLite persistence and a content-addressed HTTP cache.

Reports, market bars, claim outcomes and official truth use append-only version
records with exact-replay idempotence.  Selected current tables remain only as
latest-value caches; point-in-time readers use their immutable histories.
``AuditStore`` can be bound to a decision time; in that mode every write is
checked and future information is rejected instead of being silently clipped.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .models import (
    ClaimOutcome,
    DailyBar,
    FactorObservation,
    ResearchClaim,
    ResearchReport,
    SkillSnapshot,
    TruthObservation,
    ensure_aware,
)


SCHEMA_VERSION = 15


_OUTCOME_COLUMNS = (
    "claim_id",
    "truth_source",
    "truth_available_at",
    "realized_value",
    "market_return",
    "benchmark_return",
    "error",
    "hit",
    "mature",
    "exclusion_reason",
    "evaluated_at",
    "fundamental_hit",
    "market_hit",
    "market_excess_return",
    "market_exclusion_reason",
    "market_truth_source",
    "market_benchmark_id",
    "market_benchmark_kind",
    "truth_unit",
    "truth_basis",
    "truth_change_value",
    "truth_change_basis",
)


class AuditStorageError(RuntimeError):
    """Base exception for persistence and cache failures."""


class FutureDataError(AuditStorageError):
    """Raised when an observation was not available at the decision time."""


class CacheCorruptionError(AuditStorageError):
    """Raised when an indexed cache blob is absent or has the wrong hash."""


class CacheCollisionError(AuditStorageError):
    """Raised when a deterministic cache key produces a different payload."""


def _utc_text(value: datetime) -> str:
    return ensure_aware(value).astimezone(timezone.utc).isoformat()


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return ensure_aware(parsed)


def _decimal(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


class AuditStore:
    """WAL-backed point-in-time store for reports, outcomes and factors."""

    def __init__(
        self,
        path: str | Path,
        decision_time: datetime | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.decision_time = ensure_aware(decision_time, "decision_time") if decision_time else None
        self._connection = sqlite3.connect(self.path, timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        try:
            self._create_schema()
        except BaseException:
            self._connection.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for read-only diagnostics and migrations."""

        return self._connection

    def _create_schema(self) -> None:
        self._guard_schema_version()
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reports (
                    report_id TEXT PRIMARY KEY,
                    dimension TEXT NOT NULL CHECK (dimension IN ('macro','industry','stock')),
                    subject_id TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    industry_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    broker_code TEXT NOT NULL,
                    analyst TEXT NOT NULL,
                    team TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    timestamp_quality TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    pdf_url TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    rating TEXT NOT NULL,
                    rating_change TEXT NOT NULL,
                    target_price_min TEXT,
                    target_price_max TEXT,
                    metadata_json TEXT NOT NULL,
                    pdf_sha256 TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS reports_dimension_available_idx
                    ON reports(dimension, available_at);
                CREATE INDEX IF NOT EXISTS reports_broker_available_idx
                    ON reports(broker, available_at);

                CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    report_id TEXT NOT NULL,
                    dimension TEXT NOT NULL CHECK (dimension IN ('macro','industry','stock')),
                    subject_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    direction INTEGER NOT NULL CHECK (direction IN (-1,0,1)),
                    value_min TEXT,
                    value_max TEXT,
                    unit TEXT NOT NULL,
                    benchmark TEXT NOT NULL,
                    forecast_period TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    evidence_span TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    extraction_confidence REAL NOT NULL,
                    evidence_source_kind TEXT NOT NULL DEFAULT 'legacy/unverified',
                    evidence_source_hash TEXT NOT NULL DEFAULT '',
                    evidence_parser_version TEXT NOT NULL DEFAULT '',
                    evidence_prompt_version TEXT NOT NULL DEFAULT '',
                    extractor_bundle_sha256 TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(report_id) REFERENCES reports(report_id)
                );
                CREATE INDEX IF NOT EXISTS claims_report_idx ON claims(report_id);
                CREATE INDEX IF NOT EXISTS claims_skill_idx
                    ON claims(dimension, target_type, horizon_days, available_at);

                CREATE TABLE IF NOT EXISTS truth_observations (
                    observation_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL,
                    dimension TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    forecast_period TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    basis TEXT NOT NULL DEFAULT '',
                    change_value TEXT,
                    change_basis TEXT NOT NULL DEFAULT '',
                    realized_value TEXT NOT NULL,
                    truth_source TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    first_release INTEGER NOT NULL CHECK (first_release IN (0,1)),
                    revision INTEGER NOT NULL CHECK (revision IN (0,1)),
                    content_hash TEXT NOT NULL,
                    evidence_url TEXT NOT NULL,
                    evidence_verified INTEGER NOT NULL DEFAULT 0
                        CHECK (evidence_verified IN (0,1)),
                    CHECK (first_release + revision = 1)
                );
                CREATE INDEX IF NOT EXISTS truth_observations_claim_available_idx
                    ON truth_observations(claim_id, first_release, available_at);
                CREATE INDEX IF NOT EXISTS truth_observations_locator_available_idx
                    ON truth_observations(
                        dimension, subject_id, target_type, forecast_period,
                        first_release, available_at
                    );

                CREATE TABLE IF NOT EXISTS claim_outcomes (
                    claim_id TEXT PRIMARY KEY,
                    truth_source TEXT NOT NULL,
                    truth_available_at TEXT,
                    realized_value TEXT,
                    market_return REAL,
                    benchmark_return REAL,
                    error REAL,
                    hit INTEGER,
                    mature INTEGER NOT NULL CHECK (mature IN (0,1)),
                    exclusion_reason TEXT NOT NULL,
                    evaluated_at TEXT,
                    fundamental_hit INTEGER,
                    market_hit INTEGER,
                    market_excess_return REAL,
                    market_exclusion_reason TEXT NOT NULL DEFAULT '',
                    market_truth_source TEXT NOT NULL DEFAULT '',
                    market_benchmark_id TEXT NOT NULL DEFAULT '',
                    market_benchmark_kind TEXT NOT NULL DEFAULT '',
                    truth_unit TEXT NOT NULL DEFAULT '',
                    truth_basis TEXT NOT NULL DEFAULT '',
                    truth_change_value TEXT,
                    truth_change_basis TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(claim_id) REFERENCES claims(claim_id)
                );

                CREATE TABLE IF NOT EXISTS skill_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    as_of TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    broker_display TEXT NOT NULL DEFAULT '',
                    analyst TEXT NOT NULL,
                    team TEXT NOT NULL,
                    dimension TEXT NOT NULL CHECK (dimension IN ('macro','industry','stock')),
                    target_type TEXT NOT NULL,
                    horizon_days INTEGER NOT NULL,
                    market_state TEXT NOT NULL,
                    industry_id TEXT NOT NULL,
                    posterior_skill REAL NOT NULL,
                    conservative_lower_bound REAL NOT NULL,
                    effective_sample_size REAL NOT NULL,
                    sensitivity_365 REAL,
                    sensitivity_365_lower_bound REAL,
                    sensitivity_365_effective_sample_size REAL,
                    sensitivity_delta REAL,
                    source_report_ids_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS skill_snapshots_natural_idx ON skill_snapshots(
                    as_of, broker, analyst, team, dimension, target_type,
                    horizon_days, market_state, industry_id
                );

                CREATE TABLE IF NOT EXISTS factor_observations (
                    as_of TEXT NOT NULL,
                    stock_id TEXT NOT NULL,
                    macro_objective_factor REAL,
                    macro_report_raw REAL,
                    macro_report_factor REAL,
                    industry_objective_factor REAL,
                    industry_report_raw REAL,
                    industry_report_factor REAL,
                    stock_objective_factor REAL,
                    stock_report_raw REAL,
                    stock_report_factor REAL,
                    macro_industry_interaction REAL,
                    industry_stock_interaction REAL,
                    source_snapshot_hash TEXT NOT NULL,
                    PRIMARY KEY(as_of, stock_id)
                );

                CREATE TABLE IF NOT EXISTS daily_bars (
                    instrument_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    open TEXT NOT NULL,
                    high TEXT NOT NULL,
                    low TEXT NOT NULL,
                    close TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    amount TEXT,
                    adjusted_open TEXT,
                    adjusted_high TEXT,
                    adjusted_low TEXT,
                    adjusted_close TEXT,
                    suspended INTEGER NOT NULL CHECK (suspended IN (0,1)),
                    available_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY(instrument_id, trade_date, source)
                );
                CREATE INDEX IF NOT EXISTS daily_bars_available_idx
                    ON daily_bars(instrument_id, available_at);
                """
            )
            self._ensure_column("claim_outcomes", "fundamental_hit", "INTEGER")
            self._ensure_column("claim_outcomes", "market_hit", "INTEGER")
            self._ensure_column("claim_outcomes", "market_excess_return", "REAL")
            self._ensure_column(
                "claim_outcomes", "market_exclusion_reason", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claim_outcomes", "market_truth_source", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claim_outcomes", "market_benchmark_id", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claim_outcomes", "market_benchmark_kind", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claim_outcomes", "truth_unit", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claim_outcomes", "truth_basis", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column("claim_outcomes", "truth_change_value", "TEXT")
            self._ensure_column(
                "claim_outcomes", "truth_change_basis", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "truth_observations", "unit", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "truth_observations", "basis", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column("truth_observations", "change_value", "TEXT")
            self._ensure_column(
                "truth_observations", "change_basis", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "truth_observations",
                "evidence_verified",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                "factor_observations", "stock_objective_factor", "REAL"
            )
            self._ensure_column("factor_observations", "macro_report_raw", "REAL")
            self._ensure_column("factor_observations", "industry_report_raw", "REAL")
            self._ensure_column("factor_observations", "stock_report_raw", "REAL")
            self._ensure_column("skill_snapshots", "sensitivity_365", "REAL")
            self._ensure_column(
                "skill_snapshots", "broker_display", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "skill_snapshots", "sensitivity_365_lower_bound", "REAL"
            )
            self._ensure_column(
                "skill_snapshots", "sensitivity_365_effective_sample_size", "REAL"
            )
            self._ensure_column("skill_snapshots", "sensitivity_delta", "REAL")
            self._ensure_column(
                "reports", "pdf_sha256", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claims",
                "evidence_source_kind",
                "TEXT NOT NULL DEFAULT 'legacy/unverified'",
            )
            self._ensure_column(
                "claims", "evidence_source_hash", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claims", "evidence_parser_version", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claims", "evidence_prompt_version", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "claims", "extractor_bundle_sha256", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_outcome_version_table()
            # V10 briefly indexed report history without ``pdf_sha256``.  PDF
            # enrichment can arrive after listing ingestion with the same
            # report timestamps/content hash, so that older index would
            # silently discard the evidence-bearing version.
            self._connection.execute(
                "DROP INDEX IF EXISTS report_versions_identity_idx"
            )
            self._ensure_version_table(
                "reports",
                "report_versions",
                (
                    "report_id",
                    "content_hash",
                    "fetched_at",
                    "available_at",
                    "pdf_sha256",
                ),
            )
            self._ensure_version_table(
                "daily_bars",
                "daily_bar_versions",
                (
                    "instrument_id",
                    "trade_date",
                    "source",
                    "content_hash",
                    "fetched_at",
                    "available_at",
                ),
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS report_versions_asof_idx
                    ON report_versions(report_id, available_at, fetched_at)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS daily_bar_versions_asof_idx
                    ON daily_bar_versions(
                        instrument_id, trade_date, source, available_at, fetched_at
                    )
                """
            )
            self._connection.execute(
                """
                INSERT INTO audit_meta(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def _guard_schema_version(self) -> None:
        """Reject databases produced by an unknown future schema.

        Silently opening a newer database with older code can reinterpret an
        append-only record layout and destroy point-in-time replay.  Missing
        metadata is accepted for pre-versioned legacy databases, but malformed
        or future metadata fails closed before any migration statements run.
        """

        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_meta'"
        ).fetchone()
        if table is None:
            return
        try:
            row = self._connection.execute(
                "SELECT value FROM audit_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise AuditStorageError("cannot read audit schema metadata") from exc
        if row is None:
            return
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise AuditStorageError("invalid audit schema version") from exc
        if version < 0 or version > SCHEMA_VERSION:
            raise AuditStorageError(
                f"unsupported audit schema version {version}; "
                f"this code supports up to {SCHEMA_VERSION}"
            )

    @staticmethod
    def _outcome_version_record(row: Sequence[Any]) -> tuple[Any, ...]:
        if len(row) != len(_OUTCOME_COLUMNS):
            raise AuditStorageError("invalid claim outcome storage payload")
        payload = {name: value for name, value in zip(_OUTCOME_COLUMNS, row)}
        version_id = sha256(_json(payload).encode("utf-8")).hexdigest()
        evaluated_at = payload["evaluated_at"]
        evaluation_key = str(evaluated_at) if evaluated_at is not None else "<missing>"
        return (version_id, evaluation_key, *row)

    def _ensure_outcome_version_table(self) -> None:
        """Create and seed the immutable ClaimOutcome history.

        One claim has at most one immutable payload for an ``evaluated_at``
        instant.  Replaying the exact payload is idempotent; presenting a
        different result for the same instant is a deterministic-data
        collision and aborts the migration/write instead of being overwritten.
        """

        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS claim_outcome_versions (
                outcome_version_id TEXT PRIMARY KEY,
                evaluation_key TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                truth_source TEXT NOT NULL,
                truth_available_at TEXT,
                realized_value TEXT,
                market_return REAL,
                benchmark_return REAL,
                error REAL,
                hit INTEGER,
                mature INTEGER NOT NULL CHECK (mature IN (0,1)),
                exclusion_reason TEXT NOT NULL,
                evaluated_at TEXT,
                fundamental_hit INTEGER,
                market_hit INTEGER,
                market_excess_return REAL,
                market_exclusion_reason TEXT NOT NULL DEFAULT '',
                market_truth_source TEXT NOT NULL DEFAULT '',
                market_benchmark_id TEXT NOT NULL DEFAULT '',
                market_benchmark_kind TEXT NOT NULL DEFAULT '',
                truth_unit TEXT NOT NULL DEFAULT '',
                truth_basis TEXT NOT NULL DEFAULT '',
                truth_change_value TEXT,
                truth_change_basis TEXT NOT NULL DEFAULT '',
                UNIQUE(claim_id, evaluation_key)
            )
            """
        )
        required = {"outcome_version_id", "evaluation_key", *_OUTCOME_COLUMNS}
        actual = {
            str(row["name"])
            for row in self._connection.execute(
                "PRAGMA table_info(claim_outcome_versions)"
            ).fetchall()
        }
        missing = required - actual
        if missing:
            raise AuditStorageError(
                "claim outcome version schema is incomplete: "
                + ", ".join(sorted(missing))
            )
        try:
            # Create a named invariant even when a pre-existing table was not
            # created from the V14 declaration above.  Duplicate evaluation
            # instants in a partially migrated database make the unique index
            # fail, which is safer than choosing one version arbitrarily.
            self._connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    claim_outcome_versions_claim_evaluation_idx
                    ON claim_outcome_versions(claim_id, evaluation_key)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS claim_outcome_versions_asof_idx
                    ON claim_outcome_versions(
                        claim_id, evaluated_at, truth_available_at
                    )
                """
            )
        except sqlite3.IntegrityError as exc:
            raise AuditStorageError(
                "claim outcome version history violates uniqueness"
            ) from exc
        quoted = ",".join(f'"{name}"' for name in _OUTCOME_COLUMNS)
        legacy_rows = self._connection.execute(
            f"SELECT {quoted} FROM claim_outcomes"
        ).fetchall()
        placeholders = ",".join("?" for _ in range(len(_OUTCOME_COLUMNS) + 2))
        insert_sql = (
            "INSERT INTO claim_outcome_versions("
            "outcome_version_id,evaluation_key," + quoted + ") "
            f"VALUES ({placeholders}) "
            "ON CONFLICT(outcome_version_id) DO NOTHING"
        )
        try:
            for legacy in legacy_rows:
                values = tuple(legacy[name] for name in _OUTCOME_COLUMNS)
                version_row = self._outcome_version_record(values)
                self._connection.execute(insert_sql, version_row)
                present = self._connection.execute(
                    "SELECT 1 FROM claim_outcome_versions "
                    "WHERE outcome_version_id = ?",
                    (version_row[0],),
                ).fetchone()
                if present is None:
                    raise AuditStorageError(
                        f"failed to migrate claim outcome {values[0]}"
                    )
        except sqlite3.IntegrityError as exc:
            raise AuditStorageError(
                "conflicting claim outcome history during schema migration"
            ) from exc

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        existing = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            self._connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def _ensure_version_table(
        self,
        current_table: str,
        version_table: str,
        identity_columns: Sequence[str],
    ) -> None:
        """Create an append-only mirror and seed it from a legacy snapshot.

        The mirror is intentionally derived from the current table so additive
        schema migrations (for example, PDF evidence hashes) cannot silently
        disappear from historical versions.  Existing databases are copied
        with ``INSERT OR IGNORE`` and therefore never lose the only snapshot
        they had before versioning was introduced.
        """

        allowed = {
            ("reports", "report_versions"),
            ("daily_bars", "daily_bar_versions"),
        }
        if (current_table, version_table) not in allowed:
            raise AuditStorageError("unsupported version table mapping")
        current_columns = self._connection.execute(
            f"PRAGMA table_info({current_table})"
        ).fetchall()
        if not current_columns:
            raise AuditStorageError(f"missing source table: {current_table}")
        self._connection.execute(
            f"CREATE TABLE IF NOT EXISTS {version_table} "
            f"AS SELECT * FROM {current_table} WHERE 0"
        )
        version_columns = {
            str(row["name"])
            for row in self._connection.execute(
                f"PRAGMA table_info({version_table})"
            ).fetchall()
        }
        for column in current_columns:
            name = str(column["name"])
            if name not in version_columns:
                column_type = str(column["type"] or "TEXT")
                self._connection.execute(
                    f'ALTER TABLE {version_table} ADD COLUMN "{name}" {column_type}'
                )
        if version_table == "report_versions":
            self._connection.execute(
                "UPDATE report_versions SET pdf_sha256 = '' WHERE pdf_sha256 IS NULL"
            )
        column_names = tuple(str(row["name"]) for row in current_columns)
        quoted_columns = ",".join(f'"{name}"' for name in column_names)
        quoted_identity = ",".join(f'"{name}"' for name in identity_columns)
        self._connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {version_table}_identity_idx "
            f"ON {version_table}({quoted_identity})"
        )
        self._connection.execute(
            f"INSERT OR IGNORE INTO {version_table}({quoted_columns}) "
            f"SELECT {quoted_columns} FROM {current_table}"
        )

    def _visible(self, value: datetime | None, label: str) -> None:
        if self.decision_time is not None and value is not None and value > self.decision_time:
            raise FutureDataError(
                f"{label} {value.isoformat()} is after decision time "
                f"{self.decision_time.isoformat()}"
            )

    def _cutoff(self, explicit: datetime | None) -> datetime | None:
        if explicit is not None:
            ensure_aware(explicit, "query cutoff")
        if self.decision_time is None:
            return explicit
        if explicit is None:
            return self.decision_time
        return min(explicit, self.decision_time)

    def upsert_report(self, report: ResearchReport) -> None:
        self.upsert_reports((report,))

    def upsert_reports(self, reports: Iterable[ResearchReport]) -> int:
        rows = []
        for report in reports:
            self._visible(report.available_at, f"report {report.report_id} available_at")
            rows.append(
                (
                    report.report_id,
                    report.dimension,
                    report.subject_id,
                    report.subject_name,
                    report.industry_id,
                    report.title,
                    report.broker,
                    report.broker_code,
                    report.analyst,
                    report.team,
                    _utc_text(report.published_at),
                    _utc_text(report.available_at),
                    _utc_text(report.fetched_at),
                    report.timestamp_quality,
                    report.source,
                    report.source_url,
                    report.pdf_url,
                    report.content_hash,
                    report.rating,
                    report.rating_change,
                    str(report.target_price_min) if report.target_price_min is not None else None,
                    str(report.target_price_max) if report.target_price_max is not None else None,
                    _json(dict(report.metadata)),
                    report.pdf_sha256,
                )
            )
        if not rows:
            return 0
        with self._connection:
            for row in rows:
                conflict = self._connection.execute(
                    """
                    SELECT pdf_sha256 FROM report_versions
                    WHERE report_id = ? AND content_hash = ?
                      AND fetched_at = ? AND available_at = ?
                      AND pdf_sha256 <> '' AND pdf_sha256 <> ?
                    LIMIT 1
                    """,
                    (row[0], row[17], row[12], row[11], row[23]),
                ).fetchone()
                if row[23] and conflict is not None:
                    raise AuditStorageError(
                        "conflicting PDF hashes for the same report version: "
                        f"{row[0]}"
                    )
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO report_versions (
                    report_id, dimension, subject_id, subject_name, industry_id,
                    title, broker, broker_code, analyst, team, published_at,
                    available_at, fetched_at, timestamp_quality, source,
                    source_url, pdf_url, content_hash, rating, rating_change,
                    target_price_min, target_price_max, metadata_json, pdf_sha256
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                rows,
            )
            self._connection.executemany(
                """
                INSERT INTO reports (
                    report_id, dimension, subject_id, subject_name, industry_id,
                    title, broker, broker_code, analyst, team, published_at,
                    available_at, fetched_at, timestamp_quality, source,
                    source_url, pdf_url, content_hash, rating, rating_change,
                    target_price_min, target_price_max, metadata_json, pdf_sha256
                ) VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                ON CONFLICT(report_id) DO UPDATE SET
                    dimension=excluded.dimension,
                    subject_id=excluded.subject_id,
                    subject_name=excluded.subject_name,
                    industry_id=excluded.industry_id,
                    title=excluded.title,
                    broker=excluded.broker,
                    broker_code=excluded.broker_code,
                    analyst=excluded.analyst,
                    team=excluded.team,
                    published_at=excluded.published_at,
                    available_at=excluded.available_at,
                    fetched_at=excluded.fetched_at,
                    timestamp_quality=excluded.timestamp_quality,
                    source=excluded.source,
                    source_url=excluded.source_url,
                    pdf_url=excluded.pdf_url,
                    content_hash=excluded.content_hash,
                    rating=excluded.rating,
                    rating_change=excluded.rating_change,
                    target_price_min=excluded.target_price_min,
                    target_price_max=excluded.target_price_max,
                    metadata_json=excluded.metadata_json,
                    pdf_sha256=excluded.pdf_sha256
                WHERE excluded.fetched_at > reports.fetched_at
                   OR (
                        excluded.fetched_at = reports.fetched_at
                        AND excluded.available_at > reports.available_at
                   )
                   OR (
                        excluded.fetched_at = reports.fetched_at
                        AND excluded.available_at = reports.available_at
                        AND excluded.content_hash > reports.content_hash
                   )
                   OR (
                        excluded.fetched_at = reports.fetched_at
                        AND excluded.available_at = reports.available_at
                        AND excluded.content_hash = reports.content_hash
                        AND reports.pdf_sha256 = ''
                        AND excluded.pdf_sha256 <> ''
                   )
                """,
                rows,
            )
        return len(rows)

    def upsert_claim(self, claim: ResearchClaim) -> None:
        self.upsert_claims((claim,))

    def upsert_claims(self, claims: Iterable[ResearchClaim]) -> int:
        rows = []
        for claim in claims:
            self._visible(claim.available_at, f"claim {claim.claim_id} available_at")
            rows.append(
                (
                    claim.claim_id,
                    claim.report_id,
                    claim.dimension,
                    claim.subject_id,
                    claim.target_type,
                    claim.direction,
                    str(claim.value_min) if claim.value_min is not None else None,
                    str(claim.value_max) if claim.value_max is not None else None,
                    claim.unit,
                    claim.benchmark,
                    claim.forecast_period,
                    claim.horizon_days,
                    _utc_text(claim.available_at),
                    claim.evidence_span,
                    claim.extractor_version,
                    claim.extraction_confidence,
                    claim.evidence_source_kind,
                    claim.evidence_source_hash,
                    claim.evidence_parser_version,
                    claim.evidence_prompt_version,
                    claim.extractor_bundle_sha256,
                )
            )
        if not rows:
            return 0
        with self._connection:
            for row in rows:
                existing = self._connection.execute(
                    """
                    SELECT claim_id, report_id, dimension, subject_id,
                           target_type, direction, value_min, value_max, unit,
                           benchmark, forecast_period, horizon_days,
                           available_at, evidence_span, extractor_version,
                           extraction_confidence, evidence_source_kind,
                           evidence_source_hash, evidence_parser_version,
                           evidence_prompt_version, extractor_bundle_sha256
                    FROM claims WHERE claim_id = ?
                    """,
                    (row[0],),
                ).fetchone()
                if existing is not None and tuple(existing) != tuple(row):
                    raise AuditStorageError(
                        "claim_id collision with a different immutable payload: "
                        f"{row[0]}"
                    )
            self._connection.executemany(
                """
                INSERT INTO claims (
                    claim_id, report_id, dimension, subject_id, target_type,
                    direction, value_min, value_max, unit, benchmark,
                    forecast_period, horizon_days, available_at, evidence_span,
                    extractor_version, extraction_confidence,
                    evidence_source_kind, evidence_source_hash,
                    evidence_parser_version, evidence_prompt_version,
                    extractor_bundle_sha256
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_id) DO NOTHING
                """,
                rows,
            )
        return len(rows)

    def insert_truth_observation(self, observation: TruthObservation) -> bool:
        """Insert one immutable truth version; return ``False`` on exact replay."""

        return self.insert_truth_observations((observation,)) == 1

    def insert_truth_observations(
        self, observations: Iterable[TruthObservation]
    ) -> int:
        """Idempotently insert immutable releases without natural-key updates."""

        rows = []
        for observation in observations:
            self._visible(
                observation.available_at,
                f"truth observation {observation.observation_id} available_at",
            )
            rows.append(
                (
                    observation.observation_id,
                    observation.claim_id,
                    observation.dimension,
                    observation.subject_id,
                    observation.target_type,
                    observation.forecast_period,
                    observation.unit,
                    observation.basis,
                    str(observation.change_value)
                    if observation.change_value is not None
                    else None,
                    observation.change_basis,
                    str(observation.realized_value),
                    observation.truth_source,
                    _utc_text(observation.available_at),
                    _utc_text(observation.fetched_at),
                    int(observation.first_release),
                    int(observation.revision),
                    observation.content_hash,
                    observation.evidence_url,
                    int(observation.evidence_verified),
                )
            )
        if not rows:
            return 0
        before = self._connection.total_changes
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO truth_observations(
                    observation_id, claim_id, dimension, subject_id, target_type,
                    forecast_period, unit, basis, change_value, change_basis,
                    realized_value, truth_source, available_at, fetched_at,
                    first_release, revision, content_hash, evidence_url,
                    evidence_verified
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(observation_id) DO NOTHING
                """,
                rows,
            )
        return self._connection.total_changes - before

    def upsert_outcome(self, outcome: ClaimOutcome) -> None:
        self.upsert_outcomes((outcome,))

    def upsert_outcomes(self, outcomes: Iterable[ClaimOutcome]) -> int:
        rows = []
        for outcome in outcomes:
            self._visible(
                outcome.truth_available_at,
                f"outcome {outcome.claim_id} truth_available_at",
            )
            self._visible(outcome.evaluated_at, f"outcome {outcome.claim_id} evaluated_at")
            rows.append(
                (
                    outcome.claim_id,
                    outcome.truth_source,
                    _utc_text(outcome.truth_available_at) if outcome.truth_available_at else None,
                    str(outcome.realized_value) if outcome.realized_value is not None else None,
                    outcome.market_return,
                    outcome.benchmark_return,
                    outcome.error,
                    int(outcome.hit) if outcome.hit is not None else None,
                    int(outcome.mature),
                    outcome.exclusion_reason,
                    _utc_text(outcome.evaluated_at) if outcome.evaluated_at else None,
                    int(outcome.fundamental_hit)
                    if outcome.fundamental_hit is not None
                    else None,
                    int(outcome.market_hit) if outcome.market_hit is not None else None,
                    outcome.market_excess_return,
                    outcome.market_exclusion_reason,
                    outcome.market_truth_source,
                    outcome.market_benchmark_id,
                    outcome.market_benchmark_kind,
                    outcome.truth_unit,
                    outcome.truth_basis,
                    str(outcome.truth_change_value)
                    if outcome.truth_change_value is not None
                    else None,
                    outcome.truth_change_basis,
                )
            )
        if not rows:
            return 0
        with self._connection:
            quoted = ",".join(f'"{name}"' for name in _OUTCOME_COLUMNS)
            placeholders = ",".join("?" for _ in range(len(_OUTCOME_COLUMNS) + 2))
            version_insert = (
                "INSERT INTO claim_outcome_versions("
                "outcome_version_id,evaluation_key," + quoted + ") "
                f"VALUES ({placeholders}) "
                "ON CONFLICT(outcome_version_id) DO NOTHING"
            )
            for row in rows:
                version_row = self._outcome_version_record(row)
                existing = self._connection.execute(
                    "SELECT outcome_version_id FROM claim_outcome_versions "
                    "WHERE claim_id = ? AND evaluation_key = ?",
                    (row[0], version_row[1]),
                ).fetchone()
                if existing is not None and existing[0] != version_row[0]:
                    raise AuditStorageError(
                        "conflicting claim outcome for the same evaluation instant: "
                        f"{row[0]} / {version_row[1]}"
                    )
                self._connection.execute(version_insert, version_row)
            self._connection.executemany(
                """
                INSERT INTO claim_outcomes(
                    claim_id, truth_source, truth_available_at, realized_value,
                    market_return, benchmark_return, error, hit, mature,
                    exclusion_reason, evaluated_at, fundamental_hit, market_hit,
                    market_excess_return, market_exclusion_reason, market_truth_source,
                    market_benchmark_id, market_benchmark_kind, truth_unit,
                    truth_basis, truth_change_value, truth_change_basis
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    truth_source=excluded.truth_source,
                    truth_available_at=excluded.truth_available_at,
                    realized_value=excluded.realized_value,
                    market_return=excluded.market_return,
                    benchmark_return=excluded.benchmark_return,
                    error=excluded.error,
                    hit=excluded.hit,
                    mature=excluded.mature,
                    exclusion_reason=excluded.exclusion_reason,
                    evaluated_at=excluded.evaluated_at,
                    fundamental_hit=excluded.fundamental_hit,
                    market_hit=excluded.market_hit,
                    market_excess_return=excluded.market_excess_return,
                    market_exclusion_reason=excluded.market_exclusion_reason,
                    market_truth_source=excluded.market_truth_source,
                    market_benchmark_id=excluded.market_benchmark_id,
                    market_benchmark_kind=excluded.market_benchmark_kind,
                    truth_unit=excluded.truth_unit,
                    truth_basis=excluded.truth_basis,
                    truth_change_value=excluded.truth_change_value,
                    truth_change_basis=excluded.truth_change_basis
                WHERE (claim_outcomes.evaluated_at IS NULL AND excluded.evaluated_at IS NOT NULL)
                   OR excluded.evaluated_at > claim_outcomes.evaluated_at
                """,
                rows,
            )
        return len(rows)

    def upsert_skill_snapshot(self, snapshot: SkillSnapshot) -> None:
        self.upsert_skill_snapshots((snapshot,))

    def upsert_skill_snapshots(self, snapshots: Iterable[SkillSnapshot]) -> int:
        rows = []
        for snapshot in snapshots:
            self._visible(snapshot.as_of, f"skill snapshot {snapshot.snapshot_id} as_of")
            rows.append(
                (
                    snapshot.snapshot_id,
                    _utc_text(snapshot.as_of),
                    snapshot.broker,
                    snapshot.broker_display,
                    snapshot.analyst,
                    snapshot.team,
                    snapshot.dimension,
                    snapshot.target_type,
                    snapshot.horizon_days,
                    snapshot.market_state,
                    snapshot.industry_id,
                    snapshot.posterior_skill,
                    snapshot.conservative_lower_bound,
                    snapshot.effective_sample_size,
                    snapshot.sensitivity_365,
                    snapshot.sensitivity_365_lower_bound,
                    snapshot.sensitivity_365_effective_sample_size,
                    snapshot.sensitivity_delta,
                    _json(snapshot.source_report_ids),
                )
            )
        if not rows:
            return 0
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO skill_snapshots(
                    snapshot_id, as_of, broker, broker_display, analyst, team, dimension,
                    target_type, horizon_days, market_state, industry_id,
                    posterior_skill, conservative_lower_bound,
                    effective_sample_size, sensitivity_365,
                    sensitivity_365_lower_bound,
                    sensitivity_365_effective_sample_size, sensitivity_delta,
                    source_report_ids_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    as_of=excluded.as_of,
                    broker=excluded.broker,
                    broker_display=excluded.broker_display,
                    analyst=excluded.analyst,
                    team=excluded.team,
                    dimension=excluded.dimension,
                    target_type=excluded.target_type,
                    horizon_days=excluded.horizon_days,
                    market_state=excluded.market_state,
                    industry_id=excluded.industry_id,
                    posterior_skill=excluded.posterior_skill,
                    conservative_lower_bound=excluded.conservative_lower_bound,
                    effective_sample_size=excluded.effective_sample_size,
                    sensitivity_365=excluded.sensitivity_365,
                    sensitivity_365_lower_bound=excluded.sensitivity_365_lower_bound,
                    sensitivity_365_effective_sample_size=excluded.sensitivity_365_effective_sample_size,
                    sensitivity_delta=excluded.sensitivity_delta,
                    source_report_ids_json=excluded.source_report_ids_json
                """,
                rows,
            )
        return len(rows)

    def upsert_factor_observation(self, observation: FactorObservation) -> None:
        self.upsert_factor_observations((observation,))

    def upsert_factor_observations(self, observations: Iterable[FactorObservation]) -> int:
        rows = []
        for item in observations:
            self._visible(item.as_of, f"factor {item.stock_id} as_of")
            rows.append(
                (
                    _utc_text(item.as_of),
                    item.stock_id,
                    item.macro_objective_factor,
                    item.macro_report_raw,
                    item.macro_report_factor,
                    item.industry_objective_factor,
                    item.industry_report_raw,
                    item.industry_report_factor,
                    item.stock_objective_factor,
                    item.stock_report_raw,
                    item.stock_report_factor,
                    item.macro_industry_interaction,
                    item.industry_stock_interaction,
                    item.source_snapshot_hash,
                )
            )
        if not rows:
            return 0
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO factor_observations(
                    as_of, stock_id, macro_objective_factor, macro_report_raw,
                    macro_report_factor, industry_objective_factor,
                    industry_report_raw, industry_report_factor,
                    stock_objective_factor, stock_report_raw, stock_report_factor,
                    macro_industry_interaction, industry_stock_interaction,
                    source_snapshot_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(as_of, stock_id) DO UPDATE SET
                    macro_objective_factor=excluded.macro_objective_factor,
                    macro_report_raw=excluded.macro_report_raw,
                    macro_report_factor=excluded.macro_report_factor,
                    industry_objective_factor=excluded.industry_objective_factor,
                    industry_report_raw=excluded.industry_report_raw,
                    industry_report_factor=excluded.industry_report_factor,
                    stock_objective_factor=excluded.stock_objective_factor,
                    stock_report_raw=excluded.stock_report_raw,
                    stock_report_factor=excluded.stock_report_factor,
                    macro_industry_interaction=excluded.macro_industry_interaction,
                    industry_stock_interaction=excluded.industry_stock_interaction,
                    source_snapshot_hash=excluded.source_snapshot_hash
                """,
                rows,
            )
        return len(rows)

    def upsert_daily_bar(self, bar: DailyBar) -> None:
        self.upsert_daily_bars((bar,))

    def upsert_daily_bars(self, bars: Iterable[DailyBar]) -> int:
        rows = []
        for bar in bars:
            self._visible(bar.available_at, f"bar {bar.instrument_id}/{bar.trade_date} available_at")
            rows.append(
                (
                    bar.instrument_id,
                    bar.trade_date.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    str(bar.amount) if bar.amount is not None else None,
                    str(bar.adjusted_open) if bar.adjusted_open is not None else None,
                    str(bar.adjusted_high) if bar.adjusted_high is not None else None,
                    str(bar.adjusted_low) if bar.adjusted_low is not None else None,
                    str(bar.adjusted_close) if bar.adjusted_close is not None else None,
                    int(bar.suspended),
                    _utc_text(bar.available_at),
                    bar.source,
                    _utc_text(bar.fetched_at),
                    bar.content_hash,
                )
            )
        if not rows:
            return 0
        with self._connection:
            self._connection.executemany(
                """
                INSERT OR IGNORE INTO daily_bar_versions
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            self._connection.executemany(
                """
                INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(instrument_id, trade_date, source) DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    adjusted_open=excluded.adjusted_open,
                    adjusted_high=excluded.adjusted_high,
                    adjusted_low=excluded.adjusted_low,
                    adjusted_close=excluded.adjusted_close,
                    suspended=excluded.suspended,
                    available_at=excluded.available_at,
                    fetched_at=excluded.fetched_at,
                    content_hash=excluded.content_hash
                WHERE excluded.fetched_at > daily_bars.fetched_at
                   OR (
                        excluded.fetched_at = daily_bars.fetched_at
                        AND excluded.available_at > daily_bars.available_at
                   )
                   OR (
                        excluded.fetched_at = daily_bars.fetched_at
                        AND excluded.available_at = daily_bars.available_at
                        AND excluded.content_hash > daily_bars.content_hash
                   )
                """,
                rows,
            )
        return len(rows)

    @staticmethod
    def _where(filters: Sequence[tuple[str, Any]]) -> tuple[str, list[Any]]:
        active = [(clause, value) for clause, value in filters if value is not None]
        if not active:
            return "", []
        return " WHERE " + " AND ".join(clause for clause, _ in active), [
            value for _, value in active
        ]

    @staticmethod
    def _report_from_row(row: sqlite3.Row) -> ResearchReport:
        return ResearchReport(
            report_id=row["report_id"],
            dimension=row["dimension"],
            subject_id=row["subject_id"],
            subject_name=row["subject_name"],
            industry_id=row["industry_id"],
            title=row["title"],
            broker=row["broker"],
            broker_code=row["broker_code"],
            analyst=row["analyst"],
            team=row["team"],
            published_at=_datetime(row["published_at"]),  # type: ignore[arg-type]
            available_at=_datetime(row["available_at"]),  # type: ignore[arg-type]
            fetched_at=_datetime(row["fetched_at"]),  # type: ignore[arg-type]
            timestamp_quality=row["timestamp_quality"],
            source=row["source"],
            source_url=row["source_url"],
            pdf_url=row["pdf_url"],
            content_hash=row["content_hash"],
            pdf_sha256=row["pdf_sha256"] or "",
            rating=row["rating"],
            rating_change=row["rating_change"],
            target_price_min=_decimal(row["target_price_min"]),
            target_price_max=_decimal(row["target_price_max"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def iter_reports(
        self,
        dimension: str | None = None,
        available_by: datetime | None = None,
        *,
        version_as_of: datetime | None = None,
    ) -> Iterator[ResearchReport]:
        """Yield one point-in-time version per report.

        Without a cutoff this remains the backwards-compatible latest read.
        A store-level decision time is always a strict version ceiling.  A
        bare ``available_by`` query on a store without a decision time retains
        the legacy migration fallback, but formal point-in-time callers must
        use a decision-bound store or pass ``version_as_of`` explicitly.
        """

        cutoff = self._cutoff(available_by)
        strict_version_cutoff = (
            self._cutoff(version_as_of)
            if version_as_of is not None
            else cutoff if self.decision_time is not None else None
        )
        version_cutoff = strict_version_cutoff
        if version_cutoff is None:
            version_cutoff = cutoff
        where, params = self._where(
            (
                ("dimension = ?", dimension),
                ("available_at <= ?", _utc_text(cutoff) if cutoff else None),
                (
                    "fetched_at <= ?",
                    _utc_text(strict_version_cutoff)
                    if strict_version_cutoff
                    else None,
                ),
            )
        )
        if version_cutoff is None or strict_version_cutoff is not None:
            ordering = (
                "fetched_at DESC, available_at DESC, "
                "CASE WHEN pdf_sha256 <> '' THEN 1 ELSE 0 END DESC, "
                "content_hash DESC, pdf_sha256 DESC"
            )
            order_params: list[Any] = []
        else:
            version_text = _utc_text(version_cutoff)
            ordering = (
                "CASE WHEN fetched_at <= ? THEN 0 ELSE 1 END, "
                "CASE WHEN fetched_at <= ? THEN fetched_at END DESC, "
                "CASE WHEN fetched_at > ? THEN fetched_at END ASC, "
                "available_at DESC, "
                "CASE WHEN pdf_sha256 <> '' THEN 1 ELSE 0 END DESC, "
                "content_hash DESC, pdf_sha256 DESC"
            )
            order_params = [version_text, version_text, version_text]
        rows = self._connection.execute(
            "WITH ranked AS ("
            "SELECT report_versions.*, "
            f"ROW_NUMBER() OVER (PARTITION BY report_id ORDER BY {ordering}) AS _version_rank "
            f"FROM report_versions{where}"
            ") SELECT * FROM ranked WHERE _version_rank = 1 "
            "ORDER BY available_at, report_id",
            [*order_params, *params],
        ).fetchall()
        for row in rows:
            yield self._report_from_row(row)

    def iter_report_versions(
        self,
        *,
        report_id: str | None = None,
        dimension: str | None = None,
        content_hash: str | None = None,
        available_by: datetime | None = None,
        fetched_by: datetime | None = None,
    ) -> Iterator[ResearchReport]:
        """Inspect immutable report versions without collapsing them."""

        available_cutoff = self._cutoff(available_by)
        fetched_cutoff = self._cutoff(fetched_by)
        where, params = self._where(
            (
                ("report_id = ?", report_id),
                ("dimension = ?", dimension),
                ("content_hash = ?", content_hash),
                (
                    "available_at <= ?",
                    _utc_text(available_cutoff) if available_cutoff else None,
                ),
                (
                    "fetched_at <= ?",
                    _utc_text(fetched_cutoff) if fetched_cutoff else None,
                ),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM report_versions{where} "
            "ORDER BY report_id, fetched_at, available_at, content_hash",
            params,
        ).fetchall()
        for row in rows:
            yield self._report_from_row(row)

    def iter_claims(
        self,
        report_id: str | None = None,
        dimension: str | None = None,
        available_by: datetime | None = None,
    ) -> Iterator[ResearchClaim]:
        cutoff = self._cutoff(available_by)
        where, params = self._where(
            (
                ("report_id = ?", report_id),
                ("dimension = ?", dimension),
                ("available_at <= ?", _utc_text(cutoff) if cutoff else None),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM claims{where} ORDER BY available_at, claim_id", params
        ).fetchall()
        for row in rows:
            yield ResearchClaim(
                claim_id=row["claim_id"],
                report_id=row["report_id"],
                dimension=row["dimension"],
                subject_id=row["subject_id"],
                target_type=row["target_type"],
                direction=int(row["direction"]),
                value_min=_decimal(row["value_min"]),
                value_max=_decimal(row["value_max"]),
                unit=row["unit"],
                benchmark=row["benchmark"],
                forecast_period=row["forecast_period"],
                horizon_days=int(row["horizon_days"]),
                available_at=_datetime(row["available_at"]),  # type: ignore[arg-type]
                evidence_span=row["evidence_span"],
                extractor_version=row["extractor_version"],
                extraction_confidence=float(row["extraction_confidence"]),
                evidence_source_kind=row["evidence_source_kind"],
                evidence_source_hash=row["evidence_source_hash"],
                evidence_parser_version=row["evidence_parser_version"],
                evidence_prompt_version=row["evidence_prompt_version"],
                extractor_bundle_sha256=row["extractor_bundle_sha256"],
            )

    def iter_truth_observations(
        self,
        *,
        claim_id: str | None = None,
        dimension: str | None = None,
        subject_id: str | None = None,
        target_type: str | None = None,
        forecast_period: str | None = None,
        truth_source: str | None = None,
        decision_time: datetime | None = None,
        first_release: bool | None = True,
    ) -> Iterator[TruthObservation]:
        """Read point-in-time truth, defaulting to first releases only.

        Pass ``first_release=None`` to inspect all stored source versions or
        ``False`` to inspect revisions only.  ``decision_time`` is always
        capped by a store-level decision time when one is configured.
        """

        if first_release is not None and type(first_release) is not bool:
            raise ValueError("first_release must be a boolean or None")
        cutoff = self._cutoff(decision_time)
        where, params = self._where(
            (
                ("claim_id = ?", claim_id),
                ("dimension = ?", dimension),
                ("subject_id = ?", subject_id),
                ("target_type = ?", target_type),
                ("forecast_period = ?", forecast_period),
                ("truth_source = ?", truth_source),
                (
                    "first_release = ?",
                    int(first_release) if first_release is not None else None,
                ),
                ("available_at <= ?", _utc_text(cutoff) if cutoff else None),
                ("fetched_at <= ?", _utc_text(cutoff) if cutoff else None),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM truth_observations{where} "
            "ORDER BY available_at, observation_id",
            params,
        ).fetchall()
        for row in rows:
            yield TruthObservation(
                observation_id=row["observation_id"],
                claim_id=row["claim_id"],
                dimension=row["dimension"],
                subject_id=row["subject_id"],
                target_type=row["target_type"],
                forecast_period=row["forecast_period"],
                unit=row["unit"],
                basis=row["basis"],
                change_value=_decimal(row["change_value"]),
                change_basis=row["change_basis"],
                realized_value=_decimal(row["realized_value"]),  # type: ignore[arg-type]
                truth_source=row["truth_source"],
                available_at=_datetime(row["available_at"]),  # type: ignore[arg-type]
                fetched_at=_datetime(row["fetched_at"]),  # type: ignore[arg-type]
                first_release=bool(row["first_release"]),
                revision=bool(row["revision"]),
                content_hash=row["content_hash"],
                evidence_url=row["evidence_url"],
                evidence_verified=bool(row["evidence_verified"]),
            )

    @staticmethod
    def _outcome_from_row(row: sqlite3.Row) -> ClaimOutcome:
        return ClaimOutcome(
            claim_id=row["claim_id"],
            truth_source=row["truth_source"],
            truth_available_at=_datetime(row["truth_available_at"]),
            realized_value=_decimal(row["realized_value"]),
            market_return=_optional_float(row["market_return"]),
            benchmark_return=_optional_float(row["benchmark_return"]),
            error=_optional_float(row["error"]),
            hit=bool(row["hit"]) if row["hit"] is not None else None,
            mature=bool(row["mature"]),
            exclusion_reason=row["exclusion_reason"],
            evaluated_at=_datetime(row["evaluated_at"]),
            fundamental_hit=(
                bool(row["fundamental_hit"])
                if row["fundamental_hit"] is not None
                else None
            ),
            market_hit=bool(row["market_hit"]) if row["market_hit"] is not None else None,
            market_excess_return=_optional_float(row["market_excess_return"]),
            market_exclusion_reason=row["market_exclusion_reason"],
            market_truth_source=row["market_truth_source"],
            market_benchmark_id=row["market_benchmark_id"],
            market_benchmark_kind=row["market_benchmark_kind"],
            truth_unit=row["truth_unit"],
            truth_basis=row["truth_basis"],
            truth_change_value=_decimal(row["truth_change_value"]),
            truth_change_basis=row["truth_change_basis"],
        )

    def iter_outcomes(
        self,
        mature: bool | None = None,
        evaluated_by: datetime | None = None,
    ) -> Iterator[ClaimOutcome]:
        """Yield the latest immutable outcome version per claim at a cutoff.

        A point-in-time query never consults the mutable ``claim_outcomes``
        cache.  Versions without ``evaluated_at`` cannot be proven visible at a
        historical decision time and are therefore excluded when a cutoff is
        active.
        """

        cutoff = self._cutoff(evaluated_by)
        cutoff_text = _utc_text(cutoff) if cutoff else None
        inner_where, params = self._where(
            (
                ("COALESCE(truth_available_at, evaluated_at) <= ?", cutoff_text),
                ("evaluated_at IS NOT NULL AND evaluated_at <= ?", cutoff_text),
            )
        )
        outer_where = " WHERE _version_rank = 1"
        if mature is not None:
            outer_where += " AND mature = ?"
            params.append(int(mature))
        rows = self._connection.execute(
            "WITH ranked AS ("
            "SELECT claim_outcome_versions.*, "
            "ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY "
            "CASE WHEN evaluated_at IS NULL THEN 0 ELSE 1 END DESC, "
            "evaluated_at DESC, outcome_version_id DESC) AS _version_rank "
            f"FROM claim_outcome_versions{inner_where}"
            f") SELECT * FROM ranked{outer_where} ORDER BY evaluated_at, claim_id",
            params,
        ).fetchall()
        for row in rows:
            yield self._outcome_from_row(row)

    def iter_outcome_versions(
        self,
        *,
        claim_id: str | None = None,
        evaluated_by: datetime | None = None,
    ) -> Iterator[ClaimOutcome]:
        """Inspect immutable outcome versions without collapsing a claim."""

        cutoff = self._cutoff(evaluated_by)
        cutoff_text = _utc_text(cutoff) if cutoff else None
        where, params = self._where(
            (
                ("claim_id = ?", claim_id),
                ("COALESCE(truth_available_at, evaluated_at) <= ?", cutoff_text),
                ("evaluated_at IS NOT NULL AND evaluated_at <= ?", cutoff_text),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM claim_outcome_versions{where} "
            "ORDER BY claim_id, evaluated_at, outcome_version_id",
            params,
        ).fetchall()
        for row in rows:
            yield self._outcome_from_row(row)

    def iter_skill_snapshots(
        self,
        as_of: datetime | None = None,
        dimension: str | None = None,
    ) -> Iterator[SkillSnapshot]:
        cutoff = self._cutoff(as_of)
        where, params = self._where(
            (
                ("as_of <= ?", _utc_text(cutoff) if cutoff else None),
                ("dimension = ?", dimension),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM skill_snapshots{where} ORDER BY as_of, snapshot_id", params
        ).fetchall()
        for row in rows:
            yield SkillSnapshot(
                snapshot_id=row["snapshot_id"],
                as_of=_datetime(row["as_of"]),  # type: ignore[arg-type]
                broker=row["broker"],
                broker_display=row["broker_display"],
                analyst=row["analyst"],
                team=row["team"],
                dimension=row["dimension"],
                target_type=row["target_type"],
                horizon_days=int(row["horizon_days"]),
                market_state=row["market_state"],
                industry_id=row["industry_id"],
                posterior_skill=float(row["posterior_skill"]),
                conservative_lower_bound=float(row["conservative_lower_bound"]),
                effective_sample_size=float(row["effective_sample_size"]),
                sensitivity_365=_optional_float(row["sensitivity_365"]),
                sensitivity_365_lower_bound=_optional_float(
                    row["sensitivity_365_lower_bound"]
                ),
                sensitivity_365_effective_sample_size=_optional_float(
                    row["sensitivity_365_effective_sample_size"]
                ),
                sensitivity_delta=_optional_float(row["sensitivity_delta"]),
                source_report_ids=tuple(json.loads(row["source_report_ids_json"])),
            )

    def iter_factor_observations(
        self,
        stock_id: str | None = None,
        as_of: datetime | None = None,
    ) -> Iterator[FactorObservation]:
        cutoff = self._cutoff(as_of)
        where, params = self._where(
            (
                ("stock_id = ?", stock_id),
                ("as_of <= ?", _utc_text(cutoff) if cutoff else None),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM factor_observations{where} ORDER BY as_of, stock_id", params
        ).fetchall()
        for row in rows:
            yield FactorObservation(
                as_of=_datetime(row["as_of"]),  # type: ignore[arg-type]
                stock_id=row["stock_id"],
                macro_objective_factor=_optional_float(row["macro_objective_factor"]),
                macro_report_raw=_optional_float(row["macro_report_raw"]),
                macro_report_factor=_optional_float(row["macro_report_factor"]),
                industry_objective_factor=_optional_float(row["industry_objective_factor"]),
                industry_report_raw=_optional_float(row["industry_report_raw"]),
                industry_report_factor=_optional_float(row["industry_report_factor"]),
                stock_objective_factor=_optional_float(row["stock_objective_factor"]),
                stock_report_raw=_optional_float(row["stock_report_raw"]),
                stock_report_factor=_optional_float(row["stock_report_factor"]),
                macro_industry_interaction=_optional_float(row["macro_industry_interaction"]),
                industry_stock_interaction=_optional_float(row["industry_stock_interaction"]),
                source_snapshot_hash=row["source_snapshot_hash"],
            )

    @staticmethod
    def _daily_bar_from_row(row: sqlite3.Row) -> DailyBar:
        return DailyBar(
            instrument_id=row["instrument_id"],
            trade_date=date.fromisoformat(row["trade_date"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=Decimal(row["volume"]),
            amount=_decimal(row["amount"]),
            adjusted_open=_decimal(row["adjusted_open"]),
            adjusted_high=_decimal(row["adjusted_high"]),
            adjusted_low=_decimal(row["adjusted_low"]),
            adjusted_close=_decimal(row["adjusted_close"]),
            suspended=bool(row["suspended"]),
            available_at=_datetime(row["available_at"]),  # type: ignore[arg-type]
            source=row["source"],
            fetched_at=_datetime(row["fetched_at"]),  # type: ignore[arg-type]
            content_hash=row["content_hash"],
        )

    def iter_daily_bars(
        self,
        instrument_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        available_by: datetime | None = None,
        *,
        source: str | None = None,
        version_as_of: datetime | None = None,
    ) -> Iterator[DailyBar]:
        """Yield the point-in-time bar version for each instrument/date/source."""

        cutoff = self._cutoff(available_by)
        strict_version_cutoff = (
            self._cutoff(version_as_of)
            if version_as_of is not None
            else cutoff if self.decision_time is not None else None
        )
        version_cutoff = strict_version_cutoff
        if version_cutoff is None:
            version_cutoff = cutoff
        where, params = self._where(
            (
                ("instrument_id = ?", instrument_id),
                ("trade_date >= ?", start_date.isoformat() if start_date else None),
                ("trade_date <= ?", end_date.isoformat() if end_date else None),
                ("source = ?", source),
                ("available_at <= ?", _utc_text(cutoff) if cutoff else None),
                (
                    "fetched_at <= ?",
                    _utc_text(strict_version_cutoff)
                    if strict_version_cutoff
                    else None,
                ),
            )
        )
        if version_cutoff is None or strict_version_cutoff is not None:
            ordering = "fetched_at DESC, available_at DESC, content_hash DESC"
            order_params: list[Any] = []
        else:
            version_text = _utc_text(version_cutoff)
            ordering = (
                "CASE WHEN fetched_at <= ? THEN 0 ELSE 1 END, "
                "CASE WHEN fetched_at <= ? THEN fetched_at END DESC, "
                "CASE WHEN fetched_at > ? THEN fetched_at END ASC, "
                "available_at DESC, content_hash DESC"
            )
            order_params = [version_text, version_text, version_text]
        rows = self._connection.execute(
            "WITH ranked AS ("
            "SELECT daily_bar_versions.*, "
            "ROW_NUMBER() OVER ("
            "PARTITION BY instrument_id, trade_date, source "
            f"ORDER BY {ordering}"
            ") AS _version_rank "
            f"FROM daily_bar_versions{where}"
            ") SELECT * FROM ranked WHERE _version_rank = 1 "
            "ORDER BY instrument_id, trade_date, source",
            [*order_params, *params],
        ).fetchall()
        for row in rows:
            yield self._daily_bar_from_row(row)

    def iter_daily_bar_versions(
        self,
        *,
        instrument_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        source: str | None = None,
        content_hash: str | None = None,
        available_by: datetime | None = None,
        fetched_by: datetime | None = None,
    ) -> Iterator[DailyBar]:
        """Inspect immutable daily-bar versions without collapsing them."""

        available_cutoff = self._cutoff(available_by)
        fetched_cutoff = self._cutoff(fetched_by)
        where, params = self._where(
            (
                ("instrument_id = ?", instrument_id),
                ("trade_date >= ?", start_date.isoformat() if start_date else None),
                ("trade_date <= ?", end_date.isoformat() if end_date else None),
                ("source = ?", source),
                ("content_hash = ?", content_hash),
                (
                    "available_at <= ?",
                    _utc_text(available_cutoff) if available_cutoff else None,
                ),
                (
                    "fetched_at <= ?",
                    _utc_text(fetched_cutoff) if fetched_cutoff else None,
                ),
            )
        )
        rows = self._connection.execute(
            f"SELECT * FROM daily_bar_versions{where} "
            "ORDER BY instrument_id, trade_date, source, fetched_at, content_hash",
            params,
        ).fetchall()
        for row in rows:
            yield self._daily_bar_from_row(row)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AuditStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class CacheEntry:
    request_key: str
    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime
    content_hash: str

    def __post_init__(self) -> None:
        ensure_aware(self.fetched_at, "fetched_at")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class ContentAddressedHttpCache:
    """HTTP response cache indexed by request and stored by SHA-256 content."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blob_root = self.root / "blobs"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.root / "http_cache.sqlite3", timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS responses (
                    request_key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    headers_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    byte_length INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS response_versions (
                    request_key TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    headers_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    byte_length INTEGER NOT NULL,
                    PRIMARY KEY(request_key, response_hash, fetched_at)
                );
                CREATE INDEX IF NOT EXISTS response_versions_asof_idx
                    ON response_versions(request_key, fetched_at);
                """
            )
            for row in self._connection.execute("SELECT * FROM responses").fetchall():
                response_hash = self._response_hash(
                    row["url"],
                    int(row["status"]),
                    row["headers_json"],
                    row["content_hash"],
                    int(row["byte_length"]),
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO response_versions(
                        request_key, response_hash, url, status, headers_json,
                        fetched_at, content_hash, byte_length
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["request_key"],
                        response_hash,
                        row["url"],
                        int(row["status"]),
                        row["headers_json"],
                        row["fetched_at"],
                        row["content_hash"],
                        int(row["byte_length"]),
                    ),
                )

    def _blob_path(self, content_hash: str) -> Path:
        return self.blob_root / content_hash[:2] / content_hash

    @staticmethod
    def _response_hash(
        url: str,
        status: int,
        headers_json: str,
        content_hash: str,
        byte_length: int,
    ) -> str:
        material = _json(
            {
                "url": url,
                "status": int(status),
                "headers": json.loads(headers_json),
                "content_hash": content_hash,
                "byte_length": int(byte_length),
            }
        )
        return sha256(material.encode("utf-8")).hexdigest()

    def _entry_from_row(self, request_key: str, row: sqlite3.Row) -> CacheEntry:
        blob_path = self._blob_path(row["content_hash"])
        if not blob_path.is_file():
            raise CacheCorruptionError(f"missing cache blob: {row['content_hash']}")
        body = blob_path.read_bytes()
        actual_hash = sha256(body).hexdigest()
        if actual_hash != row["content_hash"] or len(body) != row["byte_length"]:
            raise CacheCorruptionError(f"corrupt cache blob: {row['content_hash']}")
        return CacheEntry(
            request_key=request_key,
            url=row["url"],
            status=int(row["status"]),
            headers=json.loads(row["headers_json"]),
            body=body,
            fetched_at=_datetime(row["fetched_at"]),  # type: ignore[arg-type]
            content_hash=row["content_hash"],
        )

    def get(
        self,
        request_key: str,
        *,
        as_of: datetime | None = None,
    ) -> CacheEntry | None:
        """Return the latest response, or the exact latest mapping as of time."""

        if as_of is None:
            row = self._connection.execute(
                "SELECT * FROM responses WHERE request_key = ?", (request_key,)
            ).fetchone()
        else:
            ensure_aware(as_of, "as_of")
            row = self._connection.execute(
                """
                SELECT * FROM response_versions
                WHERE request_key = ? AND fetched_at <= ?
                ORDER BY fetched_at DESC, response_hash DESC
                LIMIT 1
                """,
                (request_key, _utc_text(as_of)),
            ).fetchone()
        if row is None:
            return None
        return self._entry_from_row(request_key, row)

    def get_version(
        self,
        request_key: str,
        *,
        content_hash: str,
        fetched_at: datetime | None = None,
    ) -> CacheEntry | None:
        """Read a response version by content hash and optional fetch time."""

        if fetched_at is not None:
            ensure_aware(fetched_at, "fetched_at")
        clauses = ["request_key = ?", "content_hash = ?"]
        params: list[Any] = [request_key, content_hash]
        if fetched_at is not None:
            clauses.append("fetched_at = ?")
            params.append(_utc_text(fetched_at))
        row = self._connection.execute(
            "SELECT * FROM response_versions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY fetched_at DESC, response_hash DESC LIMIT 1",
            params,
        ).fetchone()
        return self._entry_from_row(request_key, row) if row is not None else None

    def iter_versions(
        self,
        request_key: str,
        *,
        as_of: datetime | None = None,
    ) -> Iterator[CacheEntry]:
        """Yield all immutable mappings recorded for one request key."""

        if as_of is not None:
            ensure_aware(as_of, "as_of")
        where = "request_key = ?"
        params: list[Any] = [request_key]
        if as_of is not None:
            where += " AND fetched_at <= ?"
            params.append(_utc_text(as_of))
        rows = self._connection.execute(
            f"SELECT * FROM response_versions WHERE {where} "
            "ORDER BY fetched_at, response_hash",
            params,
        ).fetchall()
        for row in rows:
            yield self._entry_from_row(request_key, row)

    def put(
        self,
        request_key: str,
        url: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        fetched_at: datetime,
    ) -> CacheEntry:
        ensure_aware(fetched_at, "fetched_at")
        content_hash = sha256(body).hexdigest()
        headers_json = _json(dict(headers))
        response_hash = self._response_hash(
            url, int(status), headers_json, content_hash, len(body)
        )
        blob_path = self._blob_path(content_hash)
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if not blob_path.exists():
            handle = tempfile.NamedTemporaryFile(
                mode="wb", dir=blob_path.parent, prefix=".tmp-", delete=False
            )
            temporary = Path(handle.name)
            try:
                with handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, blob_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO response_versions(
                    request_key, response_hash, url, status, headers_json,
                    fetched_at, content_hash, byte_length
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    request_key,
                    response_hash,
                    url,
                    int(status),
                    headers_json,
                    _utc_text(fetched_at),
                    content_hash,
                    len(body),
                ),
            )
            self._connection.execute(
                """
                INSERT INTO responses(
                    request_key, url, status, headers_json, fetched_at,
                    content_hash, byte_length
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(request_key) DO UPDATE SET
                    url=excluded.url,
                    status=excluded.status,
                    headers_json=excluded.headers_json,
                    fetched_at=excluded.fetched_at,
                    content_hash=excluded.content_hash,
                    byte_length=excluded.byte_length
                WHERE excluded.fetched_at > responses.fetched_at
                   OR (
                        excluded.fetched_at = responses.fetched_at
                        AND excluded.content_hash > responses.content_hash
                   )
                """,
                (
                    request_key,
                    url,
                    int(status),
                    headers_json,
                    _utc_text(fetched_at),
                    content_hash,
                    len(body),
                ),
            )
        return CacheEntry(
            request_key=request_key,
            url=url,
            status=int(status),
            headers=headers,
            body=body,
            fetched_at=fetched_at,
            content_hash=content_hash,
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ContentAddressedHttpCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# Short, backwards-friendly spelling for callers and CLI code.
HttpCache = ContentAddressedHttpCache


@dataclass(frozen=True)
class ExtractionCacheEntry:
    """Cached local extraction keyed by immutable parser inputs."""

    cache_key: str
    pdf_sha256: str
    parser_version: str
    prompt_version: str
    payload: bytes
    created_at: datetime
    payload_hash: str

    def __post_init__(self) -> None:
        ensure_aware(self.created_at, "created_at")


class ExtractionCache:
    """Content-addressed cache for PDF parsing and exceptional LLM output.

    The natural key is exactly ``PDF SHA256 + parser version + prompt
    version``.  Rule-only extraction should pass an explicit value such as
    ``"none"`` for ``prompt_version``.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blob_root = self.root / "artifacts"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.root / "extraction_cache.sqlite3", timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS extractions (
                    cache_key TEXT PRIMARY KEY,
                    pdf_sha256 TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    byte_length INTEGER NOT NULL,
                    UNIQUE(pdf_sha256, parser_version, prompt_version)
                )
                """
            )

    @staticmethod
    def key_for(pdf_sha256: str, parser_version: str, prompt_version: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", pdf_sha256):
            raise ValueError("pdf_sha256 must contain 64 hexadecimal characters")
        if not parser_version.strip() or not prompt_version.strip():
            raise ValueError("parser_version and prompt_version must not be empty")
        return sha256(
            f"{pdf_sha256.lower()}\x1f{parser_version}\x1f{prompt_version}".encode("utf-8")
        ).hexdigest()

    def _blob_path(self, payload_hash: str) -> Path:
        return self.blob_root / payload_hash[:2] / payload_hash

    def get(
        self,
        pdf_sha256: str,
        parser_version: str,
        prompt_version: str,
    ) -> ExtractionCacheEntry | None:
        cache_key = self.key_for(pdf_sha256, parser_version, prompt_version)
        row = self._connection.execute(
            "SELECT * FROM extractions WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None
        path = self._blob_path(row["payload_hash"])
        if not path.is_file():
            raise CacheCorruptionError(f"missing extraction blob: {row['payload_hash']}")
        payload = path.read_bytes()
        actual_hash = sha256(payload).hexdigest()
        if actual_hash != row["payload_hash"] or len(payload) != row["byte_length"]:
            raise CacheCorruptionError(f"corrupt extraction blob: {row['payload_hash']}")
        return ExtractionCacheEntry(
            cache_key=cache_key,
            pdf_sha256=row["pdf_sha256"],
            parser_version=row["parser_version"],
            prompt_version=row["prompt_version"],
            payload=payload,
            created_at=_datetime(row["created_at"]),  # type: ignore[arg-type]
            payload_hash=row["payload_hash"],
        )

    def put(
        self,
        pdf_sha256: str,
        parser_version: str,
        prompt_version: str,
        payload: bytes | str,
        *,
        created_at: datetime | None = None,
    ) -> ExtractionCacheEntry:
        cache_key = self.key_for(pdf_sha256, parser_version, prompt_version)
        body = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        payload_hash = sha256(body).hexdigest()
        existing = self.get(pdf_sha256, parser_version, prompt_version)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                raise CacheCollisionError(
                    "same PDF/parser/prompt key produced a different extraction payload"
                )
            return existing
        timestamp = created_at or datetime.now(timezone.utc)
        ensure_aware(timestamp, "created_at")
        path = self._blob_path(payload_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            handle = tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".tmp-", delete=False
            )
            temporary = Path(handle.name)
            try:
                with handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO extractions(
                    cache_key, pdf_sha256, parser_version, prompt_version,
                    created_at, payload_hash, byte_length
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    cache_key,
                    pdf_sha256.lower(),
                    parser_version,
                    prompt_version,
                    _utc_text(timestamp),
                    payload_hash,
                    len(body),
                ),
            )
        return ExtractionCacheEntry(
            cache_key=cache_key,
            pdf_sha256=pdf_sha256.lower(),
            parser_version=parser_version,
            prompt_version=prompt_version,
            payload=body,
            created_at=timestamp,
            payload_hash=payload_hash,
        )

    def get_json(
        self,
        pdf_sha256: str,
        parser_version: str,
        prompt_version: str,
    ) -> Any | None:
        entry = self.get(pdf_sha256, parser_version, prompt_version)
        return json.loads(entry.payload.decode("utf-8")) if entry else None

    def put_json(
        self,
        pdf_sha256: str,
        parser_version: str,
        prompt_version: str,
        value: Any,
        *,
        created_at: datetime | None = None,
    ) -> ExtractionCacheEntry:
        return self.put(
            pdf_sha256,
            parser_version,
            prompt_version,
            _json(value),
            created_at=created_at,
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ExtractionCache":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
