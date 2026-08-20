"""Provider registry, validation, admission and whole-batch fallback orchestration."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .admission import evaluate_admission
from .contracts import (
    DATASET_SCHEMA_VERSIONS,
    MarketDataBatch,
    MarketDataContractError,
    MarketDataRequest,
    aware_datetime,
    canonical_json_bytes,
    sha256_bytes,
)
from .providers.base import (
    AllProvidersFailedError,
    BatchValidationError,
    MarketDataProvider,
    ProviderDisabledError,
    ProviderError,
    ProviderPayload,
    UnknownProviderError,
    UnsupportedDatasetError,
    classify_unexpected_error,
    redact_sensitive_value,
    safe_error_text,
)
from .storage import (
    MarketDataStorage,
    MarketDataStorageError,
    _REGISTRY_WRITE_PERMIT,
)
from .validation import (
    DomainValidationError,
    SchemaValidationError,
    validate_and_normalize,
    validate_market_data_batch_schema,
    validate_normalized_record_schemas,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "market_data.v1.json"
DEFAULT_STORAGE_ROOT = REPO_ROOT / "data" / "market_data"
_CONFIGURED_FACTORY_PERMIT = object()
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


class RegistryConfigurationError(ValueError):
    pass


def _strict_config_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryConfigurationError(
                f"duplicate key in market-data config: {key!r}"
            )
        result[key] = value
    return result


def _reject_nonfinite_config_number(value: str) -> None:
    raise RegistryConfigurationError(
        f"non-finite number in market-data config: {value}"
    )


def load_market_data_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = REPO_ROOT / resolved
    try:
        payload = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_config_object,
            parse_constant=_reject_nonfinite_config_number,
        )
    except RegistryConfigurationError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryConfigurationError(f"cannot load market-data config {resolved}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "market-data-config-v1":
        raise RegistryConfigurationError("unsupported market-data config schema")
    default = str(payload.get("default_provider") or "")
    allowlist = payload.get("provider_allowlist")
    providers = payload.get("providers")
    datasets = payload.get("datasets")
    if not isinstance(allowlist, list) or default not in allowlist:
        raise RegistryConfigurationError("default_provider must be in provider_allowlist")
    if not isinstance(providers, dict) or set(allowlist) - set(providers):
        raise RegistryConfigurationError("every allowlisted provider must have a policy")
    for provider_id, policy in providers.items():
        if not isinstance(policy, dict):
            raise RegistryConfigurationError(
                f"provider policy must be an object: {provider_id}"
            )
        declared_datasets = policy.get("datasets")
        allowed_by_dataset = policy.get("allowed_upstream_sources")
        if (
            not isinstance(declared_datasets, list)
            or any(not isinstance(item, str) or not item for item in declared_datasets)
            or len(set(declared_datasets)) != len(declared_datasets)
        ):
            raise RegistryConfigurationError(
                f"provider datasets must be unique non-empty strings: {provider_id}"
            )
        if not isinstance(allowed_by_dataset, dict) or set(allowed_by_dataset) != set(
            declared_datasets
        ):
            raise RegistryConfigurationError(
                f"allowed_upstream_sources must cover declared datasets exactly: {provider_id}"
            )
        for dataset_type, allowed_sources in allowed_by_dataset.items():
            if (
                not isinstance(allowed_sources, list)
                or not allowed_sources
                or any(not isinstance(item, str) or not item for item in allowed_sources)
                or len(set(allowed_sources)) != len(allowed_sources)
            ):
                raise RegistryConfigurationError(
                    "allowed upstream sources must be unique non-empty strings: "
                    f"{provider_id}/{dataset_type}"
                )
    if not isinstance(datasets, dict):
        raise RegistryConfigurationError("datasets policy must be an object")
    for dataset_type, policy in datasets.items():
        if not isinstance(policy, dict):
            raise RegistryConfigurationError(
                f"dataset policy must be an object: {dataset_type}"
            )
        expected_schema = DATASET_SCHEMA_VERSIONS.get(str(dataset_type))
        if expected_schema is None or policy.get("schema_version") != expected_schema:
            raise RegistryConfigurationError(
                f"dataset schema version is unsupported: {dataset_type}"
            )
        schema_path = policy.get("schema_path")
        implementation_status = str(policy.get("implementation_status") or "")
        if schema_path is not None:
            if not isinstance(schema_path, str) or not (REPO_ROOT / schema_path).is_file():
                raise RegistryConfigurationError(
                    f"dataset schema path is unavailable: {dataset_type}"
                )
        elif not implementation_status.startswith("not_configured"):
            raise RegistryConfigurationError(
                f"implemented dataset requires schema_path: {dataset_type}"
            )
        primary = policy.get("primary")
        if primary is not None and primary not in providers:
            raise RegistryConfigurationError(
                f"dataset primary provider is unknown: {dataset_type}/{primary}"
            )
        if (
            primary is not None
            and not implementation_status.startswith("not_configured")
            and dataset_type not in providers[primary]["datasets"]
        ):
            raise RegistryConfigurationError(
                f"dataset primary provider does not declare dataset: {dataset_type}/{primary}"
            )
        validation_providers = policy.get("validation", [])
        if not isinstance(validation_providers, list) or any(
            provider_id not in providers
            or dataset_type not in providers[provider_id]["datasets"]
            for provider_id in validation_providers
        ):
            raise RegistryConfigurationError(
                f"dataset validation providers are inconsistent: {dataset_type}"
            )
        if policy.get("synthetic_allowed") is not False:
            raise RegistryConfigurationError(
                f"dataset must reject synthetic data: {dataset_type}"
            )
    fallback = payload.get("fallback_policy")
    if not isinstance(fallback, dict) or any(
        fallback.get(field) is not False
        for field in (
            "allow_partial_primary_secondary_merge",
            "allow_secondary_fill",
            "allow_default_data",
            "allow_synthetic_data",
        )
    ):
        raise RegistryConfigurationError("fallback policy must reject partial/default/synthetic data")
    cache = payload.get("cache_policy")
    expected_key_fields = [
        "provider_id",
        "dataset_type",
        "request_fingerprint",
        "adapter_version",
        "schema_version",
    ]
    if not isinstance(cache, dict) or cache.get("cache_key_fields") != expected_key_fields:
        raise RegistryConfigurationError("cache policy does not bind the V1 evidence key")
    return payload


class MarketDataRegistry:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        storage: MarketDataStorage | None = None,
        providers: Iterable[MarketDataProvider] = (),
        _configured_factory_permit: object | None = None,
    ) -> None:
        self.config = copy.deepcopy(dict(config))
        self.storage = storage
        if self.storage is not None and self.storage.admission_config is None:
            self.storage.admission_config = dict(config)
        self._providers: dict[str, MarketDataProvider] = {}
        self._evidence_mode = (
            "configured_runtime"
            if _configured_factory_permit is _CONFIGURED_FACTORY_PERMIT
            else "test_injected"
        )
        for provider in providers:
            self.register(provider)

    @property
    def evidence_mode(self) -> str:
        """Expose the registry-issued evidence class without caller override."""

        return self._evidence_mode

    @classmethod
    def configured(
        cls,
        config_path: Path | str = DEFAULT_CONFIG_PATH,
        *,
        storage_root: Path | str | None = DEFAULT_STORAGE_ROOT,
    ) -> "MarketDataRegistry":
        from .providers import (
            AKShareProvider,
            BaoStockProvider,
            ChoiceProvider,
            EastmoneyLegacyProvider,
            TushareProvider,
        )

        config = load_market_data_config(config_path)
        if storage_root is None:
            raise RegistryConfigurationError(
                "configured runtime requires evidence storage; use an explicit local storage root"
            )
        storage = MarketDataStorage(storage_root, admission_config=config)
        return cls(
            config,
            storage=storage,
            providers=(
                BaoStockProvider(),
                ChoiceProvider(),
                TushareProvider(),
                AKShareProvider(),
                EastmoneyLegacyProvider(),
            ),
            _configured_factory_permit=_CONFIGURED_FACTORY_PERMIT,
        )

    def register(self, provider: MarketDataProvider) -> None:
        provider_id = str(provider.provider_id).strip()
        if not provider_id:
            raise RegistryConfigurationError("provider_id must not be empty")
        if provider_id in self._providers:
            raise RegistryConfigurationError(f"provider already registered: {provider_id}")
        self._providers[provider_id] = provider

    def provider(self, provider_id: str | None = None) -> MarketDataProvider:
        resolved = str(provider_id or self.config.get("default_provider") or "").strip()
        allowlist = self.config.get("provider_allowlist", [])
        if resolved not in allowlist:
            raise UnknownProviderError(f"provider is not allowlisted: {resolved!r}")
        policy = self.config.get("providers", {}).get(resolved, {})
        if not isinstance(policy, Mapping) or policy.get("enabled") is not True:
            raise ProviderDisabledError(f"provider is disabled: {resolved}")
        provider = self._providers.get(resolved)
        if provider is None:
            raise UnknownProviderError(f"provider is not registered: {resolved}")
        configured_version = str(policy.get("adapter_version") or "")
        if configured_version and configured_version != provider.adapter_version:
            raise RegistryConfigurationError(
                f"provider {resolved} adapter version differs from configured policy"
            )
        return provider

    def fetch(
        self,
        request: MarketDataRequest,
        *,
        provider_id: str | None = None,
    ) -> MarketDataBatch:
        return self._fetch(
            request,
            provider_id=provider_id,
            diagnostic_replay=False,
        )

    def fetch_diagnostic(
        self,
        request: MarketDataRequest,
        *,
        provider_id: str,
    ) -> MarketDataBatch:
        """Fetch explicitly selected Choice evidence for diagnostic use only."""

        if str(provider_id).strip() != "choice":
            raise UnsupportedDatasetError(
                "fetch_diagnostic currently requires explicit provider_id='choice'"
            )
        batch = self._fetch(
            request,
            provider_id=provider_id,
            diagnostic_replay=True,
        )
        if batch.admission_status != "validated_secondary_not_primary":
            raise BatchValidationError(
                "diagnostic Choice fetch did not retain secondary-only admission"
            )
        return batch

    @contextmanager
    def diagnostic_session(self, *, provider_id: str):
        """Open one bounded Choice read-only session for repeated diagnostics."""

        if str(provider_id).strip() != "choice":
            raise UnsupportedDatasetError(
                "diagnostic_session currently requires provider_id='choice'"
            )
        provider = self.provider(provider_id)
        session_factory = getattr(provider, "diagnostic_session", None)
        if not callable(session_factory):
            raise UnsupportedDatasetError(
                "configured Choice provider does not expose a diagnostic session"
            )
        with session_factory():
            yield self

    def _fetch(
        self,
        request: MarketDataRequest,
        *,
        provider_id: str | None,
        diagnostic_replay: bool,
    ) -> MarketDataBatch:
        provider = self.provider(provider_id)
        policy = self.config.get("providers", {}).get(provider.provider_id, {})
        declared_datasets = policy.get("datasets", ()) if isinstance(policy, Mapping) else ()
        if request.dataset_type not in declared_datasets:
            raise UnsupportedDatasetError(
                f"provider {provider.provider_id} is not configured for {request.dataset_type}"
            )
        if request.dataset_type not in provider.supported_datasets:
            raise UnsupportedDatasetError(
                f"provider {provider.provider_id} does not support {request.dataset_type}"
            )
        fingerprint = request.fingerprint(provider.provider_id, provider.adapter_version)
        schema_version = DATASET_SCHEMA_VERSIONS[request.dataset_type]
        if request.retrieval_mode == "offline_replay":
            if self.storage is None:
                raise ProviderError("offline replay requires configured validated storage")
            try:
                loader = (
                    self.storage.load_latest_validated_for_diagnostics
                    if diagnostic_replay
                    else self.storage.load_latest_validated
                )
                prior, raw_content, _path = loader(
                    provider_id=provider.provider_id,
                    dataset_type=request.dataset_type,
                    request_fingerprint=fingerprint,
                    adapter_version=provider.adapter_version,
                    schema_version=schema_version,
                    fetched_at_max=(
                        request.evidence_cutoff_at or request.requested_at
                    ),
                )
            except MarketDataStorageError as exc:
                raise ProviderError(str(exc)) from exc
            if request.requested_at < prior.fetched_at:
                error = BatchValidationError(
                    "offline replay requested_at cannot precede the source batch fetched_at"
                )
                self._quarantine_error(request, provider, fingerprint, error)
                raise error
            payload = ProviderPayload(
                raw_content=raw_content,
                records=tuple(dict(item) for item in prior.records),
                fetched_at=prior.fetched_at,
                upstream_source=prior.upstream_source,
                issues=tuple(dict(item) for item in prior.issues)
                + (
                    {
                        "code": "offline_replay",
                        "severity": "info",
                        "message": f"replayed validated batch {prior.batch_id}",
                        "details": {
                            "source_batch_id": prior.batch_id,
                            "source_retrieval_mode": prior.retrieval_mode,
                            "source_requested_at": prior.requested_at.isoformat(),
                            "source_point_in_time_status": prior.point_in_time_status,
                            "source_fetched_at": prior.fetched_at.isoformat(),
                        },
                    },
                ),
            )
            replay_request = MarketDataRequest(
                dataset_type=request.dataset_type,
                requested_at=prior.requested_at,
                retrieval_mode="offline_replay",
                instrument_id=request.instrument_id,
                start_date=request.start_date,
                end_date=request.end_date,
                adjustment=request.adjustment,
                parameters=dict(request.parameters),
                evidence_cutoff_at=None,
            )
            return self._build_batch(
                replay_request,
                provider,
                fingerprint,
                payload,
                persist=False,
                replay_origin=prior,
            )

        try:
            payload = provider.fetch(request)
        except Exception as exc:
            error = classify_unexpected_error(exc)
            self._quarantine_error(request, provider, fingerprint, error)
            if error is exc:
                raise
            raise error from exc
        if not isinstance(payload, ProviderPayload):
            error = BatchValidationError(
                "provider must return the versioned ProviderPayload contract"
            )
            self._quarantine_error(request, provider, fingerprint, error)
            raise error
        return self._build_batch(request, provider, fingerprint, payload, persist=True)

    def _build_batch(
        self,
        request: MarketDataRequest,
        provider: MarketDataProvider,
        fingerprint: str,
        payload: ProviderPayload,
        *,
        persist: bool,
        replay_origin: MarketDataBatch | None = None,
    ) -> MarketDataBatch:
        decision = evaluate_admission(
            request,
            provider_id=provider.provider_id,
            upstream_source=payload.upstream_source,
            synthetic=False,
            config=self.config,
        )
        if decision.admission_status.startswith("rejected_"):
            codes = ", ".join(
                str(issue.get("code") or "policy_rejected") for issue in decision.issues
            )
            error = BatchValidationError(
                f"provider policy rejected batch: {codes}",
                raw_content=payload.raw_content,
            )
            self._quarantine_error(request, provider, fingerprint, error)
            raise error
        try:
            fetched_at = aware_datetime(payload.fetched_at, "provider.fetched_at")
        except MarketDataContractError as exc:
            error = BatchValidationError(str(exc), raw_content=payload.raw_content)
            self._quarantine_error(request, provider, fingerprint, error)
            raise error from exc
        if (
            request.retrieval_mode == "live_capture"
            and request.dataset_type in {"daily_bar", "trade_calendar"}
            and request.start_date is not None
            and request.start_date < fetched_at.astimezone(CHINA_TZ).date()
        ):
            error = BatchValidationError(
                "live_capture cannot retrieve a window containing historical dates; "
                "use historical_backfill",
                raw_content=payload.raw_content,
            )
            self._quarantine_error(request, provider, fingerprint, error)
            raise error
        try:
            records = validate_and_normalize(request, payload.records)
            validate_normalized_record_schemas(request.dataset_type, records)
        except (
            DomainValidationError,
            MarketDataContractError,
            SchemaValidationError,
        ) as exc:
            error = BatchValidationError(str(exc), raw_content=payload.raw_content)
            self._quarantine_error(request, provider, fingerprint, error)
            raise error from exc
        normalized_raw = canonical_json_bytes(list(records))
        raw_hash = sha256_bytes(payload.raw_content)
        normalized_hash = sha256_bytes(normalized_raw)
        available_values = sorted(
            aware_datetime(record["available_at"], "record.available_at")
            for record in records
        )
        issues = tuple(
            redact_sensitive_value(dict(item)) for item in payload.issues
        ) + tuple(redact_sensitive_value(dict(item)) for item in decision.issues)
        admission_status = decision.admission_status
        if self._evidence_mode != "configured_runtime":
            issues += (
                {
                    "code": "test_injected_not_formal_evidence",
                    "severity": "warning",
                    "message": "test-injected provider output is not formal research evidence",
                },
            )
        point_in_time_status = (
            replay_origin.point_in_time_status
            if replay_origin is not None
            else decision.point_in_time_status
        )
        batch_core = {
            "provider_id": provider.provider_id,
            "dataset_type": request.dataset_type,
            "request_fingerprint": fingerprint,
            "retrieval_mode": request.retrieval_mode,
            "requested_at": request.requested_at.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "raw_content_sha256": raw_hash,
            "normalized_content_sha256": normalized_hash,
        }
        batch_id = (
            f"{provider.provider_id}-{request.dataset_type}-"
            f"{sha256_bytes(canonical_json_bytes(batch_core))[:24]}"
        )
        try:
            batch = MarketDataBatch(
                batch_id=batch_id,
                provider_id=provider.provider_id,
                upstream_source=payload.upstream_source,
                dataset_type=request.dataset_type,
                schema_version=DATASET_SCHEMA_VERSIONS[request.dataset_type],
                adapter_version=provider.adapter_version,
                request_fingerprint=fingerprint,
                request_payload=request.fingerprint_payload(
                    provider.provider_id, provider.adapter_version
                ),
                retrieval_mode=request.retrieval_mode,
                requested_at=request.requested_at,
                fetched_at=fetched_at,
                available_at_min=available_values[0],
                available_at_max=available_values[-1],
                raw_content_sha256=raw_hash,
                normalized_content_sha256=normalized_hash,
                record_count=len(records),
                completeness_status="complete",
                freshness_status=decision.freshness_status,
                admission_status=admission_status,
                point_in_time_status=point_in_time_status,
                synthetic=False,
                issues=issues,
                records=records,
            )
            validate_market_data_batch_schema(batch.to_dict())
        except (MarketDataContractError, SchemaValidationError) as exc:
            error = BatchValidationError(str(exc), raw_content=payload.raw_content)
            self._quarantine_error(request, provider, fingerprint, error)
            raise error from exc
        if persist and self.storage is not None:
            self.storage.persist_validated(
                batch,
                payload.raw_content,
                _registry_write_permit=_REGISTRY_WRITE_PERMIT,
                _registry_metadata={
                    "evidence_mode": self._evidence_mode,
                    "provider_adapter_identity": (
                        f"{type(provider).__module__}.{type(provider).__qualname__}"
                    ),
                },
            )
        return batch

    def _quarantine_error(
        self,
        request: MarketDataRequest,
        provider: MarketDataProvider,
        fingerprint: str,
        error: ProviderError,
    ) -> None:
        if self.storage is None:
            return
        issue = {
            "code": error.code,
            "severity": "error",
            "message": safe_error_text(error),
        }
        try:
            paths = self.storage.persist_quarantine(
                provider_id=provider.provider_id,
                dataset_type=request.dataset_type,
                request_fingerprint=fingerprint,
                adapter_version=provider.adapter_version,
                schema_version=DATASET_SCHEMA_VERSIONS[request.dataset_type],
                requested_at=request.requested_at.isoformat(),
                retrieval_mode=request.retrieval_mode,
                raw_content=error.raw_content,
                issues=(issue,),
            )
        except Exception as quarantine_exc:
            setattr(error, "quarantine_error", safe_error_text(quarantine_exc))
            return
        setattr(error, "quarantine", paths)

    def fetch_with_fallback(
        self,
        request: MarketDataRequest,
        *,
        provider_ids: Iterable[str] | None = None,
    ) -> MarketDataBatch:
        candidates = tuple(provider_ids or (str(self.config.get("default_provider")),))
        attempts: list[Mapping[str, Any]] = []
        for provider_id in candidates:
            try:
                return self.fetch(request, provider_id=provider_id)
            except Exception as exc:
                error = classify_unexpected_error(exc)
                attempts.append(
                    {
                        "provider_id": provider_id,
                        "status": error.status,
                        "code": error.code,
                        "message": safe_error_text(error),
                    }
                )
        raise AllProvidersFailedError(tuple(attempts))


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_STORAGE_ROOT",
    "MarketDataRegistry",
    "RegistryConfigurationError",
    "load_market_data_config",
]
