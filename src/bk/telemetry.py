"""Public, UI-independent telemetry interfaces for collectors and visualizers."""

from __future__ import annotations

from typing import Optional

from .collector_status import COLLECTOR_STATUS_SCHEMA_VERSION
from .config import Config, load_config
from .usage_api import UsageQueryService
from .usage_schema import USAGE_API_VERSION
from .usage_store import (
    CollectorStatusSink,
    TelemetrySink,
    UsageAuditStore,
    UsageRetentionPolicy,
    VersionedUsageStore,
)
from .workload import WorkloadDescriptor, describe_workload


TELEMETRY_INGEST_SCHEMA_VERSION = "gpubk.telemetry.v1"


def open_usage_store(config: Optional[Config] = None) -> UsageAuditStore:
    effective = config or load_config()
    return UsageAuditStore(
        effective.data_dir,
        effective.lock_timeout_seconds,
        effective.file_mode,
        effective.dir_mode,
        effective.storage_gid,
    )


def open_usage_query(config: Optional[Config] = None) -> UsageQueryService:
    effective = config or load_config()
    return UsageQueryService(effective, open_usage_store(effective))


__all__ = [
    "TELEMETRY_INGEST_SCHEMA_VERSION",
    "COLLECTOR_STATUS_SCHEMA_VERSION",
    "USAGE_API_VERSION",
    "CollectorStatusSink",
    "TelemetrySink",
    "UsageAuditStore",
    "VersionedUsageStore",
    "UsageQueryService",
    "UsageRetentionPolicy",
    "WorkloadDescriptor",
    "describe_workload",
    "open_usage_query",
    "open_usage_store",
]
