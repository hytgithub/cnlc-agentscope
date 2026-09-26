import type { ReactNode } from 'react';

import { getResultText, parseInput, toolArgClass, toolLabelClass } from './_shared';
import type { ToolCallWithResult, ToolRenderer } from './types';
import { Markdown } from '@/components/markdown';
import { Badge } from '@/components/ui/badge';

interface TaskToolResult {
	command?: string;
	task_id?: string;
	well_id?: string;
	execution_sequence?: number;
	execution_status?: string;
	current_step?: string | null;
	effective_override?: Record<string, unknown>;
	summary?: string;
	report_markdown?: string | null;
}

const LABELS: Record<string, string> = {
	run_well_interpretation: '单井测井解释',
	modify_well_interpretation: '解释参数修改',
	rerun_well_interpretation: '全流程重跑',
	get_interpretation_status: '解释执行状态',
	get_interpretation_report: '解释报告',
};

function resultPayload(pair: ToolCallWithResult): TaskToolResult | null {
	const metadataResult = pair.result?.metadata?.result;
	if (metadataResult && typeof metadataResult === 'object') return metadataResult as TaskToolResult;
	const text = getResultText(pair.result);
	if (!text) return null;
	try {
		const value: unknown = JSON.parse(text);
		return value && typeof value === 'object' ? value as TaskToolResult : null;
	} catch {
		return null;
	}
}

function renderHeader(pair: ToolCallWithResult): ReactNode {
	const input = parseInput(pair.call.input) as Record<string, unknown>;
	const payload = resultPayload(pair);
	const argument = payload?.well_id
		?? (typeof input.well_id === 'string' ? input.well_id : payload?.task_id)
		?? '任务';
	return (
		<>
			<span className={toolLabelClass}>{LABELS[pair.call.name] ?? '测井解释'}</span>
			<span className={toolArgClass}>{argument}</span>
		</>
	);
}

function renderBody(pair: ToolCallWithResult): ReactNode {
	const payload = resultPayload(pair);
	if (!payload) return null;
	const changed = Object.entries(payload.effective_override ?? {})
		.filter(([, value]) => value !== null && value !== undefined)
		.map(([key, value]) => `${key.toUpperCase()} ${String(value)}`);
	return (
		<div className="space-y-2 rounded-sm border bg-background p-2 text-xs">
			<div className="flex flex-wrap items-center gap-2">
				{payload.execution_sequence && <span className="font-medium">Execution #{payload.execution_sequence}</span>}
				{payload.execution_status && <Badge variant="secondary">{payload.execution_status}</Badge>}
				{payload.current_step && <Badge variant="outline">{payload.current_step}</Badge>}
			</div>
			{changed.length > 0 && payload.command === 'MODIFY' && <div>{changed.join(' · ')}</div>}
			{payload.summary && <div className="text-muted-foreground">{payload.summary}</div>}
			{payload.execution_status === 'QUEUED' && <div className="text-muted-foreground">任务已提交</div>}
			{payload.report_markdown && <Markdown>{payload.report_markdown}</Markdown>}
		</div>
	);
}

export const RunWellInterpretationRenderer: ToolRenderer = {
	getDisplayName: (call) => LABELS[call.name] ?? '测井解释',
	renderHeader,
	renderBody,
};
