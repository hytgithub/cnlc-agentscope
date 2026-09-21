import { ChevronRight } from 'lucide-react';
import type { ReactNode } from 'react';

import { getResultText, parseInput, toolArgClass, toolLabelClass } from './_shared';
import type { ToolCallWithResult, ToolRenderer } from './types';
import { Markdown } from '@/components/markdown';
import { Badge } from '@/components/ui/badge';

interface DemoStep {
	id: string;
	name: string;
	status: string;
	source: string;
	input_summary?: Record<string, unknown>;
	output_summary?: Record<string, unknown>;
	evidence?: string[];
	warnings?: string[];
}

interface DemoToolResult {
	status?: string;
	task_id?: string;
	well_id?: string;
	steps?: DemoStep[];
	summary?: string;
	report_markdown?: string;
}

function resultPayload(pair: ToolCallWithResult): DemoToolResult | null {
	const text = getResultText(pair.result);
	if (!text) return null;
	try {
		const value: unknown = JSON.parse(text);
		return value && typeof value === 'object' ? (value as DemoToolResult) : null;
	} catch {
		return null;
	}
}

function renderJsonBlock(value: unknown): ReactNode {
	return (
		<pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-sm bg-muted p-2 text-xs">
			{JSON.stringify(value ?? {}, null, 2)}
		</pre>
	);
}

function renderStepCard(step: DemoStep): ReactNode {
	return (
		<details className="rounded-sm border bg-background">
			<summary className="flex cursor-pointer list-none items-center gap-2 px-2 py-1.5 text-xs">
				<ChevronRight className="size-3 shrink-0 [[open]>&]:rotate-90" />
				<span className="font-medium">{step.id}</span>
				<span className="min-w-0 flex-1 truncate">{step.name}</span>
				<Badge variant={step.status === 'SUCCESS' ? 'secondary' : 'outline'}>
					{step.status}
				</Badge>
				<Badge variant="outline">{step.source}</Badge>
			</summary>
			<div className="space-y-2 border-t px-2 py-2 text-xs">
				<div>
					<div className="mb-1 text-muted-foreground">输入摘要</div>
					{renderJsonBlock(step.input_summary)}
				</div>
				<div>
					<div className="mb-1 text-muted-foreground">输出摘要</div>
					{renderJsonBlock(step.output_summary)}
				</div>
				{step.evidence && step.evidence.length > 0 && (
					<div>
						<div className="mb-1 text-muted-foreground">Evidence</div>
						<ul className="list-disc space-y-0.5 pl-5">
							{step.evidence.map((item, index) => <li key={`${step.id}-evidence-${index}`}>{item}</li>)}
						</ul>
					</div>
				)}
				{step.warnings && step.warnings.length > 0 && (
					<div className="text-amber-700 dark:text-amber-300">
						<div className="mb-1">Warnings</div>
						<ul className="list-disc space-y-0.5 pl-5">
							{step.warnings.map((item, index) => <li key={`${step.id}-warning-${index}`}>{item}</li>)}
						</ul>
					</div>
				)}
			</div>
		</details>
	);
}

function renderHeader(pair: ToolCallWithResult): ReactNode {
	const input = parseInput(pair.call.input) as { well_id?: unknown };
	const payload = resultPayload(pair);
	const wellId = typeof payload?.well_id === 'string'
		? payload.well_id
		: typeof input.well_id === 'string' ? input.well_id : '上传井资料';
	return (
		<>
			<span className={toolLabelClass}>单井测井解释</span>
			<span className={toolArgClass}>{wellId}</span>
		</>
	);
}

function renderBody(pair: ToolCallWithResult): ReactNode {
	const payload = resultPayload(pair);
		if (!payload) return null;
	return (
		<div className="space-y-3 rounded-sm border bg-background p-2 text-xs">
			<div className="flex flex-wrap items-center gap-2">
				<span className="font-medium">W01–W10 解释过程</span>
				{payload.status && <Badge variant="secondary">{payload.status}</Badge>}
				{payload.task_id && <span className="text-muted-foreground">任务：{payload.task_id}</span>}
			</div>
			{payload.summary && <p className="text-muted-foreground">{payload.summary}</p>}
			<div className="space-y-1.5">
				{(payload.steps ?? []).map((step) => (
					<div key={step.id}>{renderStepCard(step)}</div>
				))}
			</div>
			{payload.report_markdown && (
				<div className="space-y-1 border-t pt-3">
					<div className="font-medium">Markdown Report</div>
					<Markdown>{payload.report_markdown}</Markdown>
				</div>
			)}
		</div>
	);
}

export const RunWellInterpretationRenderer: ToolRenderer = {
	getDisplayName: () => '单井测井解释',
	renderHeader,
	renderBody,
};
