"""Shared workflow vocabulary; do not introduce competing status names."""

from enum import StrEnum


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SKIPPED = "SKIPPED"


class StepId(StrEnum):
    W01 = "W01"
    W02 = "W02"
    W03 = "W03"
    W04 = "W04"
    W05 = "W05"
    W06 = "W06"
    W07 = "W07"
    W08 = "W08"
    W09 = "W09"
    W10 = "W10"


class ValidationStatus(StrEnum):
    CONSISTENT = "CONSISTENT"
    PARTIAL_CONFLICT = "PARTIAL_CONFLICT"
    SERIOUS_CONFLICT = "SERIOUS_CONFLICT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
