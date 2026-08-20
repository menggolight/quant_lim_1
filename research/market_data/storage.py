"""Raw, quarantine and validated evidence storage for market-data batches."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .admission import evaluate_admission
from .contracts import (
    MarketDataBatch,
    MarketDataContractError,
    MarketDataRequest,
    aware_datetime,
    canonical_json_bytes,
    sha256_bytes,
)
from .validation import (
    SchemaValidationError,
    validate_and_normalize,
    validate_market_data_batch_schema,
    validate_normalized_record_schemas,
)


class MarketDataStorageError(RuntimeError):
    pass


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
RESEARCH_READABLE_ADMISSIONS = frozenset(
    {"validated_research_only", "admitted_for_research"}
)
DIAGNOSTIC_READABLE_ADMISSIONS = frozenset(
    set(RESEARCH_READABLE_ADMISSIONS) | {"validated_secondary_not_primary"}
)
NON_VALIDATED_ADMISSIONS = frozenset(
    {
        "rejected_synthetic",
        "rejected_provider_not_allowlisted",
        "rejected_provider_disabled",
        "rejected_provider_dataset_undeclared",
        "rejected_unexpected_upstream",
        "quarantined",
        "not_admitted",
        "failed",
    }
)
_REGISTRY_WRITE_PERMIT = object()
_REGISTRY_RECEIPT_VERSION = "market-data-registry-receipt-v1"
_REGISTRY_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "writer",
        "batch_id",
        "batch_file_sha256",
        "raw_content_sha256",
        "admission_policy_sha256",
        "evidence_mode",
        "provider_adapter_identity",
    }
)
_CONFIGURED_PROVIDER_IDENTITIES = {
    "baostock": "research.market_data.providers.baostock.BaoStockProvider",
    "choice": "research.market_data.providers.choice.ChoiceProvider",
    "tushare": "research.market_data.providers.tushare.TushareProvider",
    "akshare": "research.market_data.providers.akshare.AKShareProvider",
    "eastmoney_legacy": (
        "research.market_data.providers.eastmoney_legacy.EastmoneyLegacyProvider"
    ),
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MarketDataStorageError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise MarketDataStorageError(f"non-finite JSON number is not allowed: {value}")


def _segment(value: str, field_name: str) -> str:
    text = str(value).strip()
    if _SAFE_SEGMENT.fullmatch(text) is None:
        raise MarketDataStorageError(f"unsafe {field_name}: {text!r}")
    return text


class MarketDataStorage:
    """Append-only evidence paths; quarantine is never a research read path."""

    def __init__(
        self,
        root: Path | str,
        *,
        admission_config: Mapping[str, Any] | None = None,
        allow_test_receipts: bool = False,
    ) -> None:
        if type(allow_test_receipts) is not bool:
            raise MarketDataStorageError("allow_test_receipts must be a boolean")
        self.root = Path(root).resolve()
        self.admission_config = (
            copy.deepcopy(dict(admission_config))
            if admission_config is not None
            else None
        )
        self.allow_test_receipts = allow_test_receipts

    @staticmethod
    def cache_key(
        provider_id: str,
        dataset_type: str,
        request_fingerprint: str,
        adapter_version: str,
        schema_version: str,
    ) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "provider_id": provider_id,
                    "dataset_type": dataset_type,
                    "request_fingerprint": request_fingerprint,
                    "adapter_version": adapter_version,
                    "schema_version": schema_version,
                }
            )
        )

    def _bucket(
        self,
        layer: str,
        provider_id: str,
        dataset_type: str,
        cache_key: str,
    ) -> Path:
        if layer not in {"raw", "quarantine", "validated"}:
            raise MarketDataStorageError("unknown storage layer")
        return (
            self.root
            / layer
            / _segment(provider_id, "provider_id")
            / _segment(dataset_type, "dataset_type")
            / _segment(cache_key, "cache_key")
        )

    @staticmethod
    def _atomic_write(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != raw:
                raise MarketDataStorageError(f"refusing to replace non-identical evidence: {path}")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".md-", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Linking a fully flushed same-directory inode publishes it
                # atomically without replacing a concurrent writer's target.
                # The deliberately short temporary name also avoids pushing a
                # valid final Windows path over MAX_PATH only during staging.
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise MarketDataStorageError(
                        f"refusing to replace non-identical evidence: {path}"
                    )
        finally:
            temporary.unlink(missing_ok=True)

    def persist_validated(
        self,
        batch: MarketDataBatch,
        raw_content: bytes,
        *,
        _registry_write_permit: object | None = None,
        _registry_metadata: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        if _registry_write_permit is not _REGISTRY_WRITE_PERMIT:
            raise MarketDataStorageError(
                "validated batches must be persisted through MarketDataRegistry"
            )
        if not isinstance(_registry_metadata, Mapping):
            raise MarketDataStorageError("MarketDataRegistry write metadata is missing")
        evidence_mode = str(_registry_metadata.get("evidence_mode") or "")
        provider_identity = str(
            _registry_metadata.get("provider_adapter_identity") or ""
        )
        if evidence_mode not in {"configured_runtime", "test_injected"}:
            raise MarketDataStorageError("unsupported registry evidence mode")
        if not provider_identity:
            raise MarketDataStorageError("provider adapter identity is missing")
        if evidence_mode == "configured_runtime" and provider_identity != (
            _CONFIGURED_PROVIDER_IDENTITIES.get(batch.provider_id)
        ):
            raise MarketDataStorageError(
                "configured registry provider adapter identity does not match provider_id"
            )
        # `validated/` is a trust boundary.  Re-run the same local checks used
        # by research readers before creating either evidence file so callers
        # cannot mint a readable batch by invoking storage directly.
        self._validate_batch(
            batch,
            raw_content,
            readable_admissions=None,
            consumer_label="validated storage",
            evidence_mode=evidence_mode,
        )
        key = self.cache_key(
            batch.provider_id,
            batch.dataset_type,
            batch.request_fingerprint,
            batch.adapter_version,
            batch.schema_version,
        )
        raw_path = self._bucket("raw", batch.provider_id, batch.dataset_type, key) / (
            _segment(batch.batch_id, "batch_id") + ".raw"
        )
        batch_path = self._bucket("validated", batch.provider_id, batch.dataset_type, key) / (
            _segment(batch.batch_id, "batch_id") + ".json"
        )
        receipt_path = self.validated_receipt_path(batch_path)
        batch_raw = canonical_json_bytes(batch.to_dict())
        receipt = {
            "receipt_version": _REGISTRY_RECEIPT_VERSION,
            "writer": "MarketDataRegistry",
            "batch_id": batch.batch_id,
            "batch_file_sha256": sha256_bytes(batch_raw),
            "raw_content_sha256": batch.raw_content_sha256,
            "admission_policy_sha256": self._admission_policy_sha256(),
            "evidence_mode": evidence_mode,
            "provider_adapter_identity": provider_identity,
        }
        self._atomic_write(raw_path, raw_content)
        self._atomic_write(batch_path, batch_raw)
        self._atomic_write(receipt_path, canonical_json_bytes(receipt))
        return {
            "raw_path": raw_path.as_posix(),
            "validated_path": batch_path.as_posix(),
            "receipt_path": receipt_path.as_posix(),
            "cache_key": key,
        }

    @staticmethod
    def validated_receipt_path(batch_path: Path | str) -> Path:
        return Path(batch_path).with_suffix(".receipt")

    def _current_admission_config(self) -> Mapping[str, Any]:
        if self.admission_config is None:
            from .registry import load_market_data_config

            return load_market_data_config()
        return self.admission_config

    def _admission_policy_sha256(self) -> str:
        return sha256_bytes(canonical_json_bytes(self._current_admission_config()))

    def _validate_registry_receipt(
        self,
        batch_path: Path,
        batch: MarketDataBatch,
    ) -> dict[str, Any]:
        receipt_path = self.validated_receipt_path(batch_path)
        if not receipt_path.is_file():
            raise MarketDataStorageError(
                "validated batch is missing its MarketDataRegistry receipt"
            )
        receipt = self._load_json(receipt_path)
        if set(receipt) != _REGISTRY_RECEIPT_FIELDS:
            raise MarketDataStorageError("validated batch registry receipt is malformed")
        evidence_mode = receipt.get("evidence_mode")
        provider_identity = receipt.get("provider_adapter_identity")
        if evidence_mode == "test_injected" and not self.allow_test_receipts:
            raise MarketDataStorageError(
                "test-injected registry receipts are not research-readable"
            )
        expected_identity = _CONFIGURED_PROVIDER_IDENTITIES.get(batch.provider_id)
        if evidence_mode == "configured_runtime" and provider_identity != expected_identity:
            raise MarketDataStorageError(
                "configured registry receipt has the wrong provider adapter identity"
            )
        if evidence_mode not in {"configured_runtime", "test_injected"}:
            raise MarketDataStorageError("validated batch registry receipt evidence mode is invalid")
        expected = {
            "receipt_version": _REGISTRY_RECEIPT_VERSION,
            "writer": "MarketDataRegistry",
            "batch_id": batch.batch_id,
            "batch_file_sha256": sha256_bytes(batch_path.read_bytes()),
            "raw_content_sha256": batch.raw_content_sha256,
            "admission_policy_sha256": self._admission_policy_sha256(),
            "evidence_mode": evidence_mode,
            "provider_adapter_identity": provider_identity,
        }
        if receipt != expected:
            raise MarketDataStorageError(
                "validated batch registry receipt does not match batch or current policy"
            )
        return receipt

    def persist_quarantine(
        self,
        *,
        provider_id: str,
        dataset_type: str,
        request_fingerprint: str,
        adapter_version: str,
        schema_version: str,
        requested_at: str,
        retrieval_mode: str,
        raw_content: bytes,
        issues: tuple[Mapping[str, Any], ...],
    ) -> dict[str, str]:
        raw_hash = sha256_bytes(raw_content)
        key = self.cache_key(
            provider_id,
            dataset_type,
            request_fingerprint,
            adapter_version,
            schema_version,
        )
        evidence_id = sha256(
            (
                f"{key}|{requested_at}|{retrieval_mode}|{raw_hash}|"
                f"{canonical_json_bytes(list(issues)).decode('utf-8')}"
            ).encode("utf-8")
        ).hexdigest()
        raw_path = self._bucket("raw", provider_id, dataset_type, key) / f"{evidence_id}.raw"
        quarantine_path = self._bucket("quarantine", provider_id, dataset_type, key) / f"{evidence_id}.json"
        self._atomic_write(raw_path, raw_content)
        payload = {
            "evidence_id": evidence_id,
            "provider_id": provider_id,
            "dataset_type": dataset_type,
            "request_fingerprint": request_fingerprint,
            "adapter_version": adapter_version,
            "schema_version": schema_version,
            "requested_at": requested_at,
            "retrieval_mode": retrieval_mode,
            "raw_content_sha256": raw_hash,
            "raw_path": raw_path.as_posix(),
            "admission_status": "quarantined",
            "issues": [dict(item) for item in issues],
        }
        self._atomic_write(quarantine_path, canonical_json_bytes(payload))
        return {
            "raw_path": raw_path.as_posix(),
            "quarantine_path": quarantine_path.as_posix(),
            "cache_key": key,
        }

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MarketDataStorageError(f"cannot read validated batch {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise MarketDataStorageError("validated batch must be a JSON object")
        return payload

    def load_latest_validated(
        self,
        *,
        provider_id: str,
        dataset_type: str,
        request_fingerprint: str,
        adapter_version: str,
        schema_version: str,
        fetched_at_max: Any | None = None,
    ) -> tuple[MarketDataBatch, bytes, Path]:
        """Load only a formally research-readable validated capture."""

        return self._load_latest_validated(
            provider_id=provider_id,
            dataset_type=dataset_type,
            request_fingerprint=request_fingerprint,
            adapter_version=adapter_version,
            schema_version=schema_version,
            fetched_at_max=fetched_at_max,
            diagnostic=False,
        )

    def load_latest_validated_for_diagnostics(
        self,
        *,
        provider_id: str,
        dataset_type: str,
        request_fingerprint: str,
        adapter_version: str,
        schema_version: str,
        fetched_at_max: Any | None = None,
    ) -> tuple[MarketDataBatch, bytes, Path]:
        """Load a validated secondary capture through the diagnostic boundary."""

        return self._load_latest_validated(
            provider_id=provider_id,
            dataset_type=dataset_type,
            request_fingerprint=request_fingerprint,
            adapter_version=adapter_version,
            schema_version=schema_version,
            fetched_at_max=fetched_at_max,
            diagnostic=True,
        )

    def _load_latest_validated(
        self,
        *,
        provider_id: str,
        dataset_type: str,
        request_fingerprint: str,
        adapter_version: str,
        schema_version: str,
        fetched_at_max: Any | None,
        diagnostic: bool,
    ) -> tuple[MarketDataBatch, bytes, Path]:
        key = self.cache_key(
            provider_id,
            dataset_type,
            request_fingerprint,
            adapter_version,
            schema_version,
        )
        bucket = self._bucket("validated", provider_id, dataset_type, key)
        cutoff = (
            aware_datetime(fetched_at_max, "fetched_at_max")
            if fetched_at_max is not None
            else None
        )
        candidates: list[tuple[MarketDataBatch, Path]] = []
        for path in bucket.glob("*.json") if bucket.is_dir() else ():
            raw_payload = self._load_json(path)
            try:
                validate_market_data_batch_schema(raw_payload)
                batch = MarketDataBatch.from_dict(raw_payload)
            except (MarketDataContractError, SchemaValidationError) as exc:
                raise MarketDataStorageError(
                    f"offline replay candidate contract is invalid: {exc}"
                ) from exc
            if cutoff is None or batch.fetched_at <= cutoff:
                candidates.append((batch, path))
        if not candidates:
            raise MarketDataStorageError(
                "offline replay cache miss at or before the evidence cutoff"
            )
        batch, path = max(candidates, key=lambda item: (item[0].fetched_at, item[0].batch_id))
        batch = (
            self.read_for_diagnostics(path)
            if diagnostic
            else self.read_for_research(path)
        )
        raw_path = self._bucket("raw", provider_id, dataset_type, key) / f"{batch.batch_id}.raw"
        if not raw_path.is_file():
            raise MarketDataStorageError("validated batch is missing raw evidence")
        raw = raw_path.read_bytes()
        if sha256_bytes(raw) != batch.raw_content_sha256:
            raise MarketDataStorageError("validated batch raw evidence hash mismatch")
        if sha256_bytes(canonical_json_bytes([dict(item) for item in batch.records])) != batch.normalized_content_sha256:
            raise MarketDataStorageError("validated batch normalized hash mismatch")
        return batch, raw, path

    def read_for_research(self, path: Path | str) -> MarketDataBatch:
        return self._read_validated_for_consumer(
            path,
            readable_admissions=RESEARCH_READABLE_ADMISSIONS,
            consumer_label="research",
        )

    def read_for_diagnostics(self, path: Path | str) -> MarketDataBatch:
        """Read secondary evidence without changing formal research admission."""

        return self._read_validated_for_consumer(
            path,
            readable_admissions=DIAGNOSTIC_READABLE_ADMISSIONS,
            consumer_label="diagnostic",
        )

    def _read_validated_for_consumer(
        self,
        path: Path | str,
        *,
        readable_admissions: frozenset[str],
        consumer_label: str,
    ) -> MarketDataBatch:
        resolved = Path(path).resolve()
        validated_root = (self.root / "validated").resolve()
        try:
            relative = resolved.relative_to(validated_root)
        except ValueError as exc:
            raise MarketDataStorageError(
                f"{consumer_label} consumers may only read validated paths"
            ) from exc
        if len(relative.parts) != 4:
            raise MarketDataStorageError("validated batch path does not match the storage contract")
        try:
            raw_payload = self._load_json(resolved)
            validate_market_data_batch_schema(raw_payload)
            batch = MarketDataBatch.from_dict(raw_payload)
        except (MarketDataContractError, SchemaValidationError) as exc:
            raise MarketDataStorageError(
                f"validated batch envelope fields or contract are invalid: {exc}"
            ) from exc
        key = self.cache_key(
            batch.provider_id,
            batch.dataset_type,
            batch.request_fingerprint,
            batch.adapter_version,
            batch.schema_version,
        )
        expected_relative = Path(
            _segment(batch.provider_id, "provider_id"),
            _segment(batch.dataset_type, "dataset_type"),
            key,
            _segment(batch.batch_id, "batch_id") + ".json",
        )
        if relative != expected_relative:
            raise MarketDataStorageError("validated batch path metadata does not match its content")
        receipt = self._validate_registry_receipt(resolved, batch)
        raw_path = self._bucket(
            "raw", batch.provider_id, batch.dataset_type, key
        ) / f"{batch.batch_id}.raw"
        if not raw_path.is_file():
            raise MarketDataStorageError("validated batch is missing raw evidence")
        return self._validate_batch(
            batch,
            raw_path.read_bytes(),
            readable_admissions=readable_admissions,
            consumer_label=consumer_label,
            evidence_mode=str(receipt["evidence_mode"]),
        )

    def _validate_batch(
        self,
        batch: MarketDataBatch,
        raw_content: bytes,
        *,
        readable_admissions: frozenset[str] | None,
        consumer_label: str,
        evidence_mode: str,
    ) -> MarketDataBatch:
        if batch.synthetic or batch.admission_status in NON_VALIDATED_ADMISSIONS:
            raise MarketDataStorageError(
                "batch is not eligible for validated storage"
            )
        if any(
            str(issue.get("severity") or "").strip().casefold() == "error"
            for issue in batch.issues
        ):
            raise MarketDataStorageError(
                "batch with error-severity issues is not eligible for validated storage"
            )
        if readable_admissions is not None and batch.admission_status not in readable_admissions:
            raise MarketDataStorageError(
                f"batch is not locally admitted for {consumer_label} consumption"
            )
        if sha256_bytes(raw_content) != batch.raw_content_sha256:
            raise MarketDataStorageError("validated batch raw evidence hash mismatch")
        normalized_raw = canonical_json_bytes([dict(item) for item in batch.records])
        if sha256_bytes(normalized_raw) != batch.normalized_content_sha256:
            raise MarketDataStorageError("validated batch normalized hash mismatch")
        try:
            request = _request_from_batch(batch)
            normalized_records = validate_and_normalize(request, batch.records)
            validate_normalized_record_schemas(batch.dataset_type, normalized_records)
            validate_market_data_batch_schema(batch.to_dict())
            if canonical_json_bytes(list(normalized_records)) != normalized_raw:
                raise MarketDataStorageError(
                    "validated batch records are not in canonical normalized form"
                )
            if evidence_mode == "configured_runtime":
                if batch.provider_id == "baostock":
                    from .providers.baostock import replay_baostock_raw

                    replayed_records = replay_baostock_raw(
                        request,
                        raw_content,
                        batch.fetched_at,
                    )
                    replayed_normalized = validate_and_normalize(
                        request, replayed_records
                    )
                    validate_normalized_record_schemas(
                        batch.dataset_type, replayed_normalized
                    )
                    if canonical_json_bytes(list(replayed_normalized)) != normalized_raw:
                        raise MarketDataStorageError(
                            "BaoStock raw replay does not match normalized records"
                        )
                elif batch.provider_id == "choice":
                    from .providers.choice import replay_choice_raw

                    replayed_records = replay_choice_raw(
                        request,
                        raw_content,
                        batch.fetched_at,
                    )
                    replayed_normalized = validate_and_normalize(
                        request, replayed_records
                    )
                    validate_normalized_record_schemas(
                        batch.dataset_type, replayed_normalized
                    )
                    if canonical_json_bytes(list(replayed_normalized)) != normalized_raw:
                        raise MarketDataStorageError(
                            "Choice raw replay does not match normalized records"
                        )
                elif readable_admissions is not None:
                    raise MarketDataStorageError(
                        f"{consumer_label}-readable provider lacks deterministic raw replay"
                    )
            current_config = self._current_admission_config()
            decision = evaluate_admission(
                request,
                provider_id=batch.provider_id,
                upstream_source=batch.upstream_source,
                synthetic=batch.synthetic,
                config=current_config,
            )
        except MarketDataStorageError:
            raise
        except Exception as exc:
            raise MarketDataStorageError(
                f"validated batch failed current Schema/domain/admission checks: {exc}"
            ) from exc
        if (
            batch.admission_status != decision.admission_status
            or batch.point_in_time_status != decision.point_in_time_status
            or batch.freshness_status != decision.freshness_status
        ):
            raise MarketDataStorageError(
                "validated batch admission metadata differs from current local policy"
            )
        return batch


def _request_from_batch(batch: MarketDataBatch) -> MarketDataRequest:
    payload = batch.request_payload
    try:
        return MarketDataRequest(
            dataset_type=str(payload.get("dataset_type") or ""),
            requested_at=batch.requested_at,
            retrieval_mode=batch.retrieval_mode,
            instrument_id=str(payload.get("instrument_id") or ""),
            start_date=payload.get("start_date"),  # type: ignore[arg-type]
            end_date=payload.get("end_date"),  # type: ignore[arg-type]
            adjustment=str(payload.get("adjustment") or "none"),
            parameters=payload.get("parameters", {}),  # type: ignore[arg-type]
        )
    except Exception as exc:
        raise MarketDataStorageError(
            f"batch request_payload cannot reconstruct its request: {exc}"
        ) from exc


def read_validated_batch(
    path: Path | str,
    *,
    storage_root: Path | str,
) -> MarketDataBatch:
    """Read a batch only from the caller's explicitly controlled storage root."""

    resolved = Path(path).resolve()
    return MarketDataStorage(storage_root).read_for_research(resolved)
