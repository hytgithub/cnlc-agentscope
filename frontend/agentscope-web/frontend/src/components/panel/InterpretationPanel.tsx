
import { WellLogPlot } from './WellLogPlot';
import type {
	InterpretationExecutionView,
	InterpretationTaskView,
} from '@/api/types';
import { Markdown } from '@/components/markdown';
import { useInterpretationExecution } from '@/hooks/useInterpretationTask';

interface InterpretationPanelProps {
	agentId: string | null;
	sessionId: string | null;
	task: InterpretationTaskView | undefined;
	loading: boolean;
}

function ExecutionBody({ execution }: { execution: InterpretationExecutionView }) {
	return (
		<div className="space-y-4 pb-4 text-sm">
			{execution.steps.some((step) => Object.keys(step.output_summary).length > 0) && (
				<section className="space-y-2 rounded-lg border p-3">
					<div className="font-medium">处理结果</div>
					{execution.steps
						.filter((step) => Object.keys(step.output_summary).length > 0)
						.map((step) => (
							<div key={step.name} className="space-y-1 rounded bg-muted p-2 text-xs">
								<div className="font-medium">{step.name}</div>
								<pre className="whitespace-pre-wrap">{JSON.stringify(step.output_summary, null, 2)}</pre>
							</div>
						))}
				</section>
			)}
			<WellLogPlot key={execution.execution_id} plot={execution.log_plot} executionId={execution.execution_id} />

			<section className="space-y-2 border-t pt-3">
				<div className="text-xs font-medium text-muted-foreground">最终解释报告</div>
				{execution.report_ready && execution.report_markdown
					? <Markdown>{execution.report_markdown}</Markdown>
					: <div className="text-xs text-muted-foreground">报告尚未生成</div>}
			</section>
		</div>
	);
}

/** 仅展示当前测井解释任务的结果，不承载阶段控制或执行历史。 */
export function InterpretationPanel({ agentId, sessionId, task, loading }: InterpretationPanelProps) {
	const execution = useInterpretationExecution(
		agentId,
		sessionId,
		task?.task_id ?? null,
		task?.current_execution_id ?? null,
	);

	if (loading && !task) return <div className="p-3 text-sm text-muted-foreground">正在读取执行状态…</div>;
	if (!task) return <div className="p-3 text-sm text-muted-foreground">本会话尚无测井解释任务</div>;

	return (
		<div className="space-y-4 pt-2">
			<div className="font-semibold">{task.well_name ?? task.well_id}</div>
			{execution.isLoading && <div className="text-sm text-muted-foreground">正在读取结果…</div>}
			{execution.data && <ExecutionBody execution={execution.data} />}
		</div>
	);
}
