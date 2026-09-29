import { ChevronRight } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { WellLogPlot } from './WellLogPlot';
import type {
	InterpretationExecutionView,
	InterpretationTaskView,
} from '@/api/types';
import { Markdown } from '@/components/markdown';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	useInterpretationExecution,
	useInterpretationStageLogPlot,
} from '@/hooks/useInterpretationTask';

interface InterpretationPanelProps {
	agentId: string | null;
	sessionId: string | null;
	task: InterpretationTaskView | undefined;
	loading: boolean;
}

const displayTime = (value: string | null) =>
	value ? new Date(value).toLocaleString() : '—';

const MAX_PANEL_REPORT_CHARS = 200_000;

const statusMark = (status: string) => {
	if (status === 'REUSED' || status === 'SUCCESS' || status === 'WARNING') return '✓';
	if (status === 'RUNNING') return '●';
	if (status === 'FAILED' || status === 'BLOCKED' || status === 'REVIEW_REQUIRED') return '×';
	return '○';
};

const SUMMARY_PREVIEW_ITEMS = 10;

function previewJson(value: unknown): { value: unknown; truncated: boolean } {
	if (Array.isArray(value)) {
		const children = value.slice(0, SUMMARY_PREVIEW_ITEMS).map(previewJson);
		return {
			value: children.map((child) => child.value),
			truncated: value.length > SUMMARY_PREVIEW_ITEMS || children.some((child) => child.truncated),
		};
	}
	if (value && typeof value === 'object') {
		let truncated = false;
		const entries = Object.entries(value as Record<string, unknown>).map(([key, item]) => {
			const child = previewJson(item);
			truncated ||= child.truncated;
			return [key, child.value];
		});
		return { value: Object.fromEntries(entries), truncated };
	}
	return { value, truncated: false };
}

function JsonSummary({ value }: { value: Record<string, unknown> }) {
	const [expanded, setExpanded] = useState(false);
	const preview = useMemo(() => previewJson(value), [value]);
	return (
		<div className="space-y-1">
			<pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-sm bg-muted p-2 text-xs">
				{JSON.stringify(expanded ? value : preview.value, null, 2)}
			</pre>
			{preview.truncated && (
				<Button size="sm" variant="ghost" onClick={() => setExpanded((current) => !current)}>
					{expanded ? '收起，仅展示每组前 10 条' : '查看全部数据'}
				</Button>
			)}
		</div>
	);
}

function ExecutionBody({
	agentId,
	sessionId,
	taskId,
	execution,
}: {
	agentId: string;
	sessionId: string;
	taskId: string;
	execution: InterpretationExecutionView;
}) {
	const [selectedPlotStage, setSelectedPlotStage] = useState(execution.latest_log_plot_stage);
	const [plotSelectionPinned, setPlotSelectionPinned] = useState(false);
	useEffect(() => {
		setPlotSelectionPinned(false);
		setSelectedPlotStage(execution.latest_log_plot_stage);
	}, [execution.execution_id]);
	useEffect(() => {
		setSelectedPlotStage((current) => {
			if (!plotSelectionPinned) return execution.latest_log_plot_stage;
			return current && execution.available_log_plot_stages.includes(current)
				? current
				: execution.latest_log_plot_stage;
		});
	}, [execution.available_log_plot_stages, execution.latest_log_plot_stage, plotSelectionPinned]);
	const selectedPlotAvailable = Boolean(
		selectedPlotStage && execution.available_log_plot_stages.includes(selectedPlotStage),
	);
	const selectedPlotQuery = useInterpretationStageLogPlot(
		agentId,
		sessionId,
		taskId,
		execution.execution_id,
		selectedPlotStage,
		selectedPlotAvailable,
	);
	const isMockExecution = execution.tool_runs.some((run) => run.execution_mode === 'MOCK');
	const currentStepName = execution.steps.find((step) => step.id === execution.current_step)?.name;
	const parameters = [
		['sampling_interval', execution.effective_override.sampling_interval],
		['POR', execution.effective_override.por],
		['PERM', execution.effective_override.perm],
		['prediction_model', execution.effective_override.prediction_model],
	] as const;
	const reportTruncated = Boolean(
		execution.report_markdown && execution.report_markdown.length > MAX_PANEL_REPORT_CHARS,
	);
	const visibleReport = reportTruncated
		? execution.report_markdown!.slice(0, MAX_PANEL_REPORT_CHARS)
		: execution.report_markdown;

	return (
		<div className="space-y-4 pb-4 text-sm">
			<section className="space-y-2 rounded-lg border p-3">
				<div className="flex flex-wrap items-center gap-2">
					<span className="font-semibold">Execution #{execution.sequence}</span>
					<Badge variant="secondary">{execution.execution_status}</Badge>
					<Badge variant="outline">{isMockExecution ? 'Mock 执行' : '真实执行'}</Badge>
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
							<div><div className="mb-1 text-muted-foreground">输入摘要</div><JsonSummary value={step.input_summary} /></div>
							<div><div className="mb-1 text-muted-foreground">输出摘要</div><JsonSummary value={step.output_summary} /></div>
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

			<section className="space-y-2">
				<div className="flex items-center justify-between gap-2">
					<div className="text-xs font-medium text-muted-foreground">阶段曲线产物</div>
					<select
						className="rounded border bg-background px-2 py-1 text-xs"
						value={selectedPlotStage ?? ''}
						onChange={(event) => {
							setPlotSelectionPinned(true);
							setSelectedPlotStage(event.target.value as typeof selectedPlotStage);
						}}
					>
						<option value="RAW" disabled={!execution.available_log_plot_stages.includes('RAW')}>原始 GDSX 曲线</option>
						<option value="PREPROCESSED" disabled={!execution.available_log_plot_stages.includes('PREPROCESSED')}>预处理后 GDSX 曲线</option>
						<option value="INTERPRETED" disabled={!execution.available_log_plot_stages.includes('INTERPRETED')}>智能处理后 GDSX 曲线</option>
					</select>
				</div>
				<div className="text-xs text-muted-foreground">
					阶段完成后读取一次并缓存；后台继续执行下一阶段时不会重复轮询曲线。
				</div>
				{selectedPlotQuery.isLoading && <div className="text-xs text-muted-foreground">正在读取阶段曲线…</div>}
				{selectedPlotQuery.isError && <div className="text-xs text-destructive">阶段曲线读取失败</div>}
				<WellLogPlot
					key={`${execution.execution_id}-${selectedPlotStage ?? 'empty'}`}
					plot={selectedPlotQuery.data ?? null}
					executionId={execution.execution_id}
				/>
			</section>

			<section className="space-y-2 border-t pt-3">
				<div className="text-xs font-medium text-muted-foreground">正式解释报告</div>
				{execution.official_report
					? <div className="space-y-2 rounded border p-3 text-xs">
						<div>状态：{execution.official_report.status ?? 'SUCCESS'}</div>
						<div>文件：{execution.official_report.file_name ?? '解释报告.docx'}</div>
						{execution.official_report.file_url
							? <a className="text-primary underline" href={execution.official_report.file_url} target="_blank" rel="noreferrer">预览或下载正式报告</a>
							: <div className="text-amber-700">报告服务未返回可下载地址</div>}
					</div>
					: <div className="text-xs text-muted-foreground">正式报告尚未生成</div>}
			</section>

			<section className="space-y-2 border-t pt-3">
				<div className="text-xs font-medium text-muted-foreground">执行诊断摘要（非正式报告）</div>
				{execution.report_ready && visibleReport
					? <>
						<Markdown>{visibleReport}</Markdown>
						{reportTruncated && (
							<div className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
								报告过大，右侧仅展示前 200,000 个字符；完整结构化结果仍保留在当前 Execution 中。
							</div>
						)}
					</>
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
			{selected && agentId && sessionId && (
				<ExecutionBody
					agentId={agentId}
					sessionId={sessionId}
					taskId={task.task_id}
					execution={selected}
				/>
			)}
		</div>
	);
}
