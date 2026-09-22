"""Workflow 共享枚举；各模块不得创建语义重复的状态名称。"""

from enum import StrEnum


class StepStatus(StrEnum):
    """Workflow 节点和任务统一使用的状态集合。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SKIPPED = "SKIPPED"


class StepId(StrEnum):
    """固定业务流程 W01-W10 的步骤标识。"""

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
    """多源资料综合验证结论。"""

    CONSISTENT = "CONSISTENT"
    PARTIAL_CONFLICT = "PARTIAL_CONFLICT"
    SERIOUS_CONFLICT = "SERIOUS_CONFLICT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
