"""Fail-closed Experiment V3 admission boundary.

Only a structural, self-hashed *diagnostic binding* can be represented today.
The formal controlled loader is deliberately not implemented, therefore no
object created from this module is formal admission evidence. Production
Alpha, Exposure and Constructor entry points must call
``verify_experiment_v3_admission_receipt``; that function remains fail-closed
until a separately controlled loader exists.

There is intentionally no issuer token or issuer helper in production code.
Tests may construct the diagnostic dataclass directly, but doing so cannot
make the formal verifier succeed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any

from .contracts import canonical_sha256


EXPERIMENT_V3_ADMISSION_SCHEMA_VERSION = "experiment-v3-admission-receipt.v1"
EXPERIMENT_V3_ADMISSION_STATUS = "diagnostic_binding_only_not_formally_admitted"
FORMAL_LOADER_STATUS = "blocked_not_implemented"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ExperimentV3AdmissionError(ValueError):
    """Raised when formal V3 admission is absent, malformed or mismatched."""


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExperimentV3AdmissionError(f"{field_name} must be timezone-aware")
    return value


def _sha256(value: str, field_name: str) -> str:
    normalized = str(value)
    if _SHA256_RE.fullmatch(normalized) is None:
        raise ExperimentV3AdmissionError(
            f"{field_name} must be a lowercase SHA-256"
        )
    return normalized


def _identifier(value: str, field_name: str) -> str:
    normalized = str(value).strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise ExperimentV3AdmissionError(f"{field_name} is not a valid identifier")
    return normalized


@dataclass(frozen=True, slots=True)
class ExperimentV3AdmissionReceiptV1:
    """Self-hashed diagnostic graph binding; never formal admission evidence."""

    receipt_id: str
    issued_at: datetime
    experiment_spec_sha256: str
    approved_factor_registry_sha256: str
    approved_factor_registry_frozen_at: datetime
    model_training_receipt_sha256: str
    model_admission_receipt_sha256: str
    model_sha256: str
    model_frozen_at: datetime
    calibration_receipt_sha256: str
    calibration_horizon_sessions: int
    exposure_policy_source_sha256: str
    exposure_policy_frozen_at: datetime
    constructor_policy_source_sha256: str
    constructor_policy_frozen_at: datetime
    status: str = EXPERIMENT_V3_ADMISSION_STATUS
    formal_loader_status: str = FORMAL_LOADER_STATUS
    paper_eligibility: bool = False
    trade_eligibility: bool = False
    real_money_list_allowed: bool = False
    live_supported: bool = False
    schema_version: str = EXPERIMENT_V3_ADMISSION_SCHEMA_VERSION
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != EXPERIMENT_V3_ADMISSION_SCHEMA_VERSION:
            raise ExperimentV3AdmissionError("unsupported V3 admission receipt schema")
        if self.status != EXPERIMENT_V3_ADMISSION_STATUS:
            raise ExperimentV3AdmissionError(
                "V3 receipt status must remain diagnostic and not formally admitted"
            )
        if self.formal_loader_status != FORMAL_LOADER_STATUS:
            raise ExperimentV3AdmissionError(
                "formal V3 admission loader must remain blocked"
            )
        if any(
            value is not False
            for value in (
                self.paper_eligibility,
                self.trade_eligibility,
                self.real_money_list_allowed,
                self.live_supported,
            )
        ):
            raise ExperimentV3AdmissionError(
                "V3 diagnostic binding grants no Paper, trading or LIVE authority"
            )
        object.__setattr__(self, "receipt_id", _identifier(self.receipt_id, "receipt_id"))
        issued_at = _aware(self.issued_at, "issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        for field_name in (
            "experiment_spec_sha256",
            "approved_factor_registry_sha256",
            "model_training_receipt_sha256",
            "model_admission_receipt_sha256",
            "model_sha256",
            "calibration_receipt_sha256",
            "exposure_policy_source_sha256",
            "constructor_policy_source_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _sha256(getattr(self, field_name), field_name),
            )
        evidence_times = {
            "approved_factor_registry_frozen_at": _aware(
                self.approved_factor_registry_frozen_at,
                "approved_factor_registry_frozen_at",
            ),
            "model_frozen_at": _aware(self.model_frozen_at, "model_frozen_at"),
            "exposure_policy_frozen_at": _aware(
                self.exposure_policy_frozen_at, "exposure_policy_frozen_at"
            ),
            "constructor_policy_frozen_at": _aware(
                self.constructor_policy_frozen_at, "constructor_policy_frozen_at"
            ),
        }
        if any(value > issued_at for value in evidence_times.values()):
            raise ExperimentV3AdmissionError(
                "V3 diagnostic receipt cannot precede bound frozen artifacts"
            )
        for field_name, value in evidence_times.items():
            object.__setattr__(self, field_name, value)
        if (
            type(self.calibration_horizon_sessions) is not int
            or self.calibration_horizon_sessions <= 0
        ):
            raise ExperimentV3AdmissionError(
                "calibration_horizon_sessions must be a positive integer"
            )
        object.__setattr__(
            self,
            "receipt_sha256",
            canonical_sha256(self.to_content_dict()),
        )

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "issued_at": self.issued_at.isoformat(),
            "status": self.status,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "approved_factor_registry_sha256": self.approved_factor_registry_sha256,
            "approved_factor_registry_frozen_at": self.approved_factor_registry_frozen_at.isoformat(),
            "model_training_receipt_sha256": self.model_training_receipt_sha256,
            "model_admission_receipt_sha256": self.model_admission_receipt_sha256,
            "model_sha256": self.model_sha256,
            "model_frozen_at": self.model_frozen_at.isoformat(),
            "calibration_receipt_sha256": self.calibration_receipt_sha256,
            "calibration_horizon_sessions": self.calibration_horizon_sessions,
            "exposure_policy_source_sha256": self.exposure_policy_source_sha256,
            "exposure_policy_frozen_at": self.exposure_policy_frozen_at.isoformat(),
            "constructor_policy_source_sha256": self.constructor_policy_source_sha256,
            "constructor_policy_frozen_at": self.constructor_policy_frozen_at.isoformat(),
            "formal_loader_status": self.formal_loader_status,
            "paper_eligibility": self.paper_eligibility,
            "trade_eligibility": self.trade_eligibility,
            "real_money_list_allowed": self.real_money_list_allowed,
            "live_supported": self.live_supported,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_content_dict()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload

    def require_structural_valid(
        self, *, as_of: datetime
    ) -> "ExperimentV3AdmissionReceiptV1":
        """Validate the diagnostic binding without upgrading its authority."""

        if type(self) is not ExperimentV3AdmissionReceiptV1:
            raise ExperimentV3AdmissionError(
                "V3 diagnostic receipt must use the exact controlled contract type"
            )
        checked_at = _aware(as_of, "as_of")
        if self.issued_at > checked_at:
            raise ExperimentV3AdmissionError("V3 diagnostic receipt is future-dated")
        if canonical_sha256(self.to_content_dict()) != self.receipt_sha256:
            raise ExperimentV3AdmissionError("V3 diagnostic receipt hash mismatch")
        if (
            self.status != EXPERIMENT_V3_ADMISSION_STATUS
            or self.formal_loader_status != FORMAL_LOADER_STATUS
            or any(
                value is not False
                for value in (
                    self.paper_eligibility,
                    self.trade_eligibility,
                    self.real_money_list_allowed,
                    self.live_supported,
                )
            )
        ):
            raise ExperimentV3AdmissionError("V3 diagnostic safety contract drifted")
        return self

    def require_valid(self, *, as_of: datetime) -> "ExperimentV3AdmissionReceiptV1":
        """Require formal admission; impossible while the loader is blocked."""

        ExperimentV3AdmissionReceiptV1.require_structural_valid(
            self,
            as_of=as_of,
        )
        raise ExperimentV3AdmissionError(
            "formal Experiment V3 loader is blocked_not_implemented; "
            "diagnostic binding is not admission"
        )


def verify_experiment_v3_diagnostic_binding(
    receipt: ExperimentV3AdmissionReceiptV1,
    *,
    as_of: datetime,
) -> ExperimentV3AdmissionReceiptV1:
    """Validate the exact diagnostic type without dynamic dispatch.

    This helper deliberately invokes the base implementation.  A caller-owned
    subclass cannot override a validation method and turn a diagnostic binding
    into either formal admission or risk-exit authority.
    """

    if type(receipt) is not ExperimentV3AdmissionReceiptV1:
        raise ExperimentV3AdmissionError(
            "V3 diagnostic receipt must use the exact controlled contract type"
        )
    return ExperimentV3AdmissionReceiptV1.require_structural_valid(
        receipt,
        as_of=as_of,
    )


def verify_experiment_v3_admission_receipt(
    receipt: ExperimentV3AdmissionReceiptV1,
    *,
    as_of: datetime,
    experiment_spec_sha256: str | None = None,
    approved_factor_registry_sha256: str | None = None,
    approved_factor_registry_frozen_at: datetime | None = None,
    model_training_receipt_sha256: str | None = None,
    model_admission_receipt_sha256: str | None = None,
    model_sha256: str | None = None,
    model_frozen_at: datetime | None = None,
    calibration_receipt_sha256: str | None = None,
    calibration_horizon_sessions: int | None = None,
    exposure_policy_source_sha256: str | None = None,
    exposure_policy_frozen_at: datetime | None = None,
    constructor_policy_source_sha256: str | None = None,
    constructor_policy_frozen_at: datetime | None = None,
) -> None:
    """Fail closed until formal loader-backed admission exists."""

    if type(receipt) is not ExperimentV3AdmissionReceiptV1:
        raise ExperimentV3AdmissionError(
            "policy admission receipt must be ExperimentV3AdmissionReceiptV1"
        )
    verify_experiment_v3_diagnostic_binding(receipt, as_of=as_of)
    raise ExperimentV3AdmissionError(
        "formal Experiment V3 loader is blocked_not_implemented; "
        "diagnostic binding is not admission"
    )

    # Unreachable while the formal loader is blocked. Kept as the exact future
    # binding contract; caller-supplied expected values cannot bypass admission.
    expected_hashes = {
        "experiment_spec_sha256": experiment_spec_sha256,
        "approved_factor_registry_sha256": approved_factor_registry_sha256,
        "model_training_receipt_sha256": model_training_receipt_sha256,
        "model_admission_receipt_sha256": model_admission_receipt_sha256,
        "model_sha256": model_sha256,
        "calibration_receipt_sha256": calibration_receipt_sha256,
        "exposure_policy_source_sha256": exposure_policy_source_sha256,
        "constructor_policy_source_sha256": constructor_policy_source_sha256,
    }
    for field_name, expected in expected_hashes.items():
        if expected is not None and getattr(receipt, field_name) != _sha256(
            expected, field_name
        ):
            raise ExperimentV3AdmissionError(
                f"V3 admission receipt {field_name} mismatch"
            )
    expected_times = {
        "approved_factor_registry_frozen_at": approved_factor_registry_frozen_at,
        "model_frozen_at": model_frozen_at,
        "exposure_policy_frozen_at": exposure_policy_frozen_at,
        "constructor_policy_frozen_at": constructor_policy_frozen_at,
    }
    for field_name, expected in expected_times.items():
        if expected is not None and getattr(receipt, field_name) != _aware(
            expected, field_name
        ):
            raise ExperimentV3AdmissionError(
                f"V3 admission receipt {field_name} mismatch"
            )
    if (
        calibration_horizon_sessions is not None
        and receipt.calibration_horizon_sessions != calibration_horizon_sessions
    ):
        raise ExperimentV3AdmissionError(
            "V3 admission receipt calibration_horizon_sessions mismatch"
        )


__all__ = [
    "EXPERIMENT_V3_ADMISSION_SCHEMA_VERSION",
    "EXPERIMENT_V3_ADMISSION_STATUS",
    "FORMAL_LOADER_STATUS",
    "ExperimentV3AdmissionError",
    "ExperimentV3AdmissionReceiptV1",
    "verify_experiment_v3_diagnostic_binding",
    "verify_experiment_v3_admission_receipt",
]
