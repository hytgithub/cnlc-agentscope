"""将已校验的整份操作计划映射为白名单命令；不查询仓库、不执行专业计算。"""

from collections.abc import Mapping
from dataclasses import dataclass

from cnlc_agent.application.commands import (
    FullRerunCommand,
    GetReportCommand,
    GetStatusCommand,
    ModifyInterpretationCommand,
)
from cnlc_agent.demo.operation_models import (
    ActionType,
    OperationConstraintType,
    OperationEdgeType,
    OperationNode,
    OperationPlan,
    PersistMode,
    TargetType,
    ValueMode,
    WholeWellScope,
)
from cnlc_agent.demo.reference_resolver import ResolvedExecutionReference, ResolvedTaskReference
from cnlc_agent.domain.errors import DataError
from cnlc_agent.domain.override import InterpretationOverride


@dataclass(frozen=True)
class AdaptedCommand:
    """一次命令可对应多个参数节点；节点顺序用于诊断，不预分配 Execution。"""

    operation_ids: tuple[str, ...]
    command: GetStatusCommand


def _modify_changes(op: OperationNode) -> InterpretationOverride:
    """只读取正式参数字段，extensions 永远不参与命令构建。"""

    parameters = op.parameters
    target_parameter = {
        TargetType.POROSITY: "por",
        TargetType.PERMEABILITY: "perm",
        TargetType.MODEL: "prediction_model",
    }.get(op.target)
    name = parameters.parameter_name or target_parameter
    if (target_parameter and name != target_parameter) or op.target not in {
        TargetType.POROSITY,
        TargetType.PERMEABILITY,
        TargetType.MODEL,
        TargetType.WELL,
    }:
        raise DataError("INVALID_OPERATION_PLAN", "目标与参数名称不一致或不支持")
    if name not in {"por", "perm", "sampling_interval", "prediction_model"}:
        raise DataError("UNSUPPORTED_PARAMETER", "参数必须属于既有 Override 契约")
    if parameters.method_id is not None:
        raise DataError("UNSUPPORTED_OPERATION", "参数修改不支持额外方法选择")
    if name == "prediction_model":
        if parameters.model_id is None or parameters.value is not None:
            raise DataError("INVALID_OPERATION_PLAN", "预测模型必须只提供 model_id")
        return InterpretationOverride(prediction_model=parameters.model_id)
    if parameters.model_id is not None or parameters.value is None:
        raise DataError("INVALID_OPERATION_PLAN", "数值参数必须只提供 ValueSpec")
    if parameters.value.mode != ValueMode.ABSOLUTE:
        raise DataError("UNSUPPORTED_VALUE_MODE", "当前只支持绝对值修改")
    # 数值原样交给唯一业务契约检查；不按 unit 换算或推导专业规则。
    return InterpretationOverride.model_validate({name: parameters.value.value})


def adapt_commands(
    plan: OperationPlan,
    tasks: Mapping[str, ResolvedTaskReference],
    executions: Mapping[str, ResolvedExecutionReference],
) -> list[AdaptedCommand]:
    """完整适配成功后才返回命令；除同任务多参数合并外不允许复合写。"""

    writes = [
        op for op in plan.operations if op.action not in {ActionType.STATUS, ActionType.REPORT}
    ]
    if (
        writes
        and len(plan.operations) > 1
        and not all(op.action == ActionType.MODIFY_PARAMETER for op in plan.operations)
    ):
        raise DataError("COMPOUND_EXECUTION_UNSUPPORTED", "当前执行桥不支持该复合执行")
    if writes and plan.persist_mode != PersistMode.CREATE_VERSION:
        raise DataError("UNSUPPORTED_OPERATION", "现有写命令只支持创建正式版本")
    if any(edge.type != OperationEdgeType.SEQUENCE for edge in plan.edges):
        raise DataError("COMPOUND_EXECUTION_UNSUPPORTED", "当前命令不消费节点数据或比较依赖")
    commands: dict[str, AdaptedCommand] = {}
    changes: dict[str, float | str] = {}
    write_targets: set[tuple[str, str]] = set()
    for op in plan.operations:
        if op.input_refs or any(
            constraint.type != OperationConstraintType.ONLY_SCOPE for constraint in op.constraints
        ):
            raise DataError("UNSUPPORTED_OPERATION", "现有命令无法保证指定输入或附加约束")
        if not isinstance(op.scope, WholeWellScope):
            raise DataError("UNSUPPORTED_OPERATION", "现有命令只支持整井范围")
        task = tasks[op.operation_id]
        execution = executions[op.operation_id]
        if op in writes:
            if execution.execution_id != task.current_execution_id:
                raise DataError(
                    "HISTORICAL_BASE_WRITE_UNSUPPORTED", "当前不支持从历史版本创建分支写操作"
                )
            write_targets.add((task.task_id, execution.execution_id))
        command: GetStatusCommand
        if op.action == ActionType.MODIFY_PARAMETER:
            override = _modify_changes(op)
            for name, value in override.model_dump(exclude_none=True).items():
                if name in changes and changes[name] != value:
                    raise DataError("MULTI_OPERATION_CONFLICT", "同一参数存在互相矛盾的修改值")
                changes[name] = value
            continue
        # 读和重跑不接受参数载荷；extensions 仅作元数据，绝不透传。
        if any(
            value is not None for value in op.parameters.model_dump(exclude={"extensions"}).values()
        ):
            raise DataError("INVALID_OPERATION_PLAN", "当前动作不能携带修改值或方法模型参数")
        if op.action == ActionType.FULL_RERUN:
            if op.target != TargetType.WELL:
                raise DataError("INVALID_OPERATION_PLAN", "全量重跑必须以整井为目标")
            command = FullRerunCommand(task_id=task.task_id)
        elif op.action == ActionType.STATUS and op.target in {
            TargetType.WELL,
            TargetType.EXECUTION,
        }:
            command = GetStatusCommand(task_id=task.task_id)
        elif op.action == ActionType.REPORT and op.target in {
            TargetType.WELL,
            TargetType.REPORT,
            TargetType.EXECUTION,
        }:
            command = GetReportCommand(task_id=task.task_id, execution_id=execution.execution_id)
        else:
            raise DataError("UNSUPPORTED_OPERATION", "该动作或目标没有明确的命令适配")
        commands[op.operation_id] = AdaptedCommand((op.operation_id,), command)
    if changes:
        if len(write_targets) != 1:
            raise DataError("COMPOUND_EXECUTION_UNSUPPORTED", "参数聚合要求同一任务及当前版本")
        task_id, _ = next(iter(write_targets))
        return [
            AdaptedCommand(
                tuple(op.operation_id for op in plan.operations),
                ModifyInterpretationCommand(
                    task_id=task_id, changes=InterpretationOverride.model_validate(changes)
                ),
            )
        ]
    # 只排序操作关系；专业 Workflow 依赖仍由 Application 管理。
    pending = list(plan.operations)
    ordered: list[AdaptedCommand] = []
    done: set[str] = set()
    while pending:
        ready = next(
            (
                op
                for op in pending
                if all(
                    edge.from_operation_id in done
                    for edge in plan.edges
                    if edge.to_operation_id == op.operation_id
                )
            ),
            None,
        )
        if ready is None:
            raise ValueError("validated operation graph must be acyclic")
        ordered.append(commands[ready.operation_id])
        done.add(ready.operation_id)
        pending.remove(ready)
    return ordered
