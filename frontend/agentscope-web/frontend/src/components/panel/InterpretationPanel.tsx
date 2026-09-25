import { ChevronRight } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import type {
	InterpretationExecutionView,
	InterpretationTaskView,
} from '@/api/types';
import { Markdown } from '@/components/markdown';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useInterpretationExecution } from '@/hooks/useInterpretationTask';

interface InterpretationPanelProps {
	agentId: string | null;
	sessionId: string | null;
	task: InterpretationTaskView | undefined;
	loading: boolean;
}

const displayTime = (value: string | null) =>
	value ? new Date(value).toLocaleString() : '—';

const statusMark = (status: string) => {
	if (status === 'REUSED' || status === 'SUCCESS' || status === 'WARNING') return '✓';
	if (status === 'RUNNING') return '●';
	if (status === 'FAILED' || status === 'BLOCKED' || status === 'REVIEW_REQUIRED') return '×';
	return '○';
};

const jsonSummary = (value: Record<string, unknown>) => (
	<pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-sm bg-muted p-2 text-xs">
		{JSON.stringify(value, null, 2)}
	</pre>
);

function ExecutionBody({ execution }: { execution: InterpretationExecutionView }) {
	const currentStepName = execution.steps.find((step) => step.id === execution.current_step)?.name;
	const parameters = [
		['sampling_interval', execution.effective_override.sampling_interval],
		['POR', execution.effective_override.por],
		['PERM', execution.effective_override.perm],
		['prediction_model', execution.effective_override.prediction_model],
	] as const;

	return (
		<div className="space-y-4 pb-4 text-sm">
			<section className="space-y-2 rounded-lg border p-3">
				<div className="flex flex-wrap items-center gap-2">
					<span className="font-semibold">Execution #{execution.sequence}</span>
					<Badge variant="secondary">{execution.execution_status}</Badge>
					<Badge variant="outline">Demo / Mock</Badge>
				</div>
				<div className="text-xs text-muted-foreground">
					{execution.execution_status === 'QUEUED'
						? '等待执行'
						: execution.current_step
							? `当前步骤：${execution.current_step} ${currentStepName ?? ''}`
							: `已完成：${execution.completed_steps.length} / 10`}
				</div>
				<div className="text-xs text-muted-foreground">开始时间：{displayTime(execution.started_at)}</div>
				{execution.error_code && (
					<div className="text-xs text-destructive">错误代码：{execution.error_code}</div>
				)}
			</section>

			<section className="space-y-2">
				<div className="text-xs font-medium text-muted-foreground">执行阶段</div>
				<div className="grid grid-cols-2 gap-1.5">
					{execution.stages.map((stage) => (
						<div key={stage.stage} className="flex items-center justify-between rounded border px-2 py-1.5 text-xs">
							<span>{stage.name}</span>
							<Badge variant={stage.action === 'REUSE' ? 'outline' : 'secondary'}>{stage.action}</Badge>
						</div>
					))}
				</div>
			</section>

			<section className="space-y-1.5">
				<div className="text-xs font-medium text-muted-foreground">W01–W10</div>
				{execution.steps.map((step) => (
					<details key={step.id} className="rounded border bg-background">
						<summary className="flex cursor-pointer list-none items-center gap-2 px-2 py-1.5 text-xs">
							<ChevronRight className="size-3 shrink-0 [[open]>&]:rotate-90" />
							<span className="w-3 text-center">{statusMark(step.display_status)}</span>
							<span className="font-medium">{step.id}</span>
							<span className="min-w-0 flex-1 truncate">{step.name}</span>
							<Badge variant="outline">{step.display_status}</Badge>
						</summary>
						<div className="space-y-2 border-t p-2 text-xs">
							<div>来源：{step.source}</div>
							<div><div className="mb-1 text-muted-foreground">输入摘要</div>{jsonSummary(step.input_summary)}</div>
							<div><div className="mb-1 text-muted-foreground">输出摘要</div>{jsonSummary(step.output_summary)}</div>
							{step.evidence.length > 0 && <div>证据：{step.evidence.join('；')}</div>}
							{step.warnings.length > 0 && <div className="text-amber-700 dark:text-amber-300">告警：{step.warnings.join('；')}</div>}
						</div>
					</details>
				))}
			</section>

			<details className="rounded border bg-background">
				<summary className="cursor-pointer px-2 py-2 text-xs font-medium">专业工具调用（{execution.tool_runs.length}）</summary>
				<div className="space-y-1.5 border-t p-2">
					{execution.tool_runs.length === 0 && <div className="text-xs text-muted-foreground">本版本没有专业工具调用</div>}
					{execution.tool_runs.map((run) => (
						<div key={run.tool_run_id} className="rounded bg-muted p-2 text-xs">
							<div className="flex flex-wrap items-center gap-1.5">
								<span className="font-medium">{run.step_id} {run.tool_code}</span>
								<Badge variant="outline">{run.execution_mode}</Badge>
								<Badge variant="secondary">{run.status}</Badge>
							</div>
							<div className="mt-1 text-muted-foreground">来源：{run.source}</div>
							{run.error_code && <div className="mt-1 text-destructive">错误代码：{run.error_code}</div>}
						</div>
					))}
				</div>
			</details>

			<section className="space-y-1.5">
				<div className="text-xs font-medium text-muted-foreground">有效参数</div>
				<div className="grid grid-cols-2 gap-1.5 text-xs">
					{parameters.map(([name, value]) => (
						<div key={name} className="rounded border px-2 py-1.5">
							<div className="text-muted-foreground">{name}</div>
							<div>{value ?? '默认'}</div>
						</div>
					))}
				</div>
			</section>

			<section className="space-y-2 border-t pt-3">
				<div className="text-xs font-medium text-muted-foreground">Markdown Report</div>
				{execution.report_ready && execution.report_markdown
					? <Markdown>{execution.report_markdown}</Markdown>
					: <div className="text-xs text-muted-foreground">报告尚未生成</div>}
			</section>
		</div>
	);
}

/** 测井解释详情及历史选择；历史选择使用独立状态，不会被当前轮询覆盖。 */
export function InterpretationPanel({ agentId, sessionId, task, loading }: InterpretationPanelProps) {
	const [selectedExecutionId, setSelectedExecutionId] = useState<string | null>(null);
	useEffect(() => setSelectedExecutionId(null), [sessionId, task?.task_id]);
	const historicalId = selectedExecutionId === task?.current_execution_id
		? null
		: selectedExecutionId;
	const historical = useInterpretationExecution(
		agentId,
		sessionId,
		task?.task_id ?? null,
		historicalId,
	);
	const selected = useMemo(
		() => historicalId ? historical.data : task?.current_execution,
		[historicalId, historical.data, task?.current_execution],
	);

	if (loading && !task) return <div className="p-3 text-sm text-muted-foreground">正在读取执行状态…</div>;
	if (!task) return <div className="p-3 text-sm text-muted-foreground">本会话尚无测井解释任务</div>;

	return (
		<div className="space-y-4 pt-2">
			<div className="flex items-center justify-between gap-2">
				<div>
					<div className="font-semibold">{task.well_id}</div>
					<div className="max-w-56 truncate text-xs text-muted-foreground" title={task.task_id}>{task.task_id}</div>
				</div>
				<Button size="sm" variant={!historicalId ? 'secondary' : 'outline'} onClick={() => setSelectedExecutionId(null)}>
					当前
				</Button>
			</div>

			<section className="space-y-1.5">
				<div className="text-xs font-medium text-muted-foreground">Execution History</div>
				<div className="flex flex-wrap gap-1.5">
					{task.executions.map((execution) => {
						const current = execution.execution_id === task.current_execution_id;
						const selectedHistory = historicalId === execution.execution_id || (!historicalId && current);
						return (
							<Button
								key={execution.execution_id}
								size="sm"
								variant={selectedHistory ? 'secondary' : 'outline'}
								onClick={() => setSelectedExecutionId(current ? null : execution.execution_id)}
							>
								#{execution.sequence} {execution.execution_status}{current ? ' · 当前' : ''}
							</Button>
						);
					})}
				</div>
			</section>

			{historical.isLoading && <div className="text-xs text-muted-foreground">正在读取历史版本…</div>}
			{selected && <ExecutionBody execution={selected} />}
		</div>
	);
}
