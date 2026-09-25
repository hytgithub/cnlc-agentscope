import type { Msg, ToolResultBlock } from '@agentscope-ai/agentscope/message';
import { useQuery } from '@tanstack/react-query';

import { interpretationApi } from '@/api';

const TASK_TOOLS = new Set([
	'run_well_interpretation',
	'modify_well_interpretation',
	'rerun_well_interpretation',
	'get_interpretation_status',
	'get_interpretation_report',
]);

function resultPayload(block: ToolResultBlock): Record<string, unknown> | null {
	const metadataResult = block.metadata?.result;
	if (metadataResult && typeof metadataResult === 'object') {
		return metadataResult as Record<string, unknown>;
	}
	const texts = typeof block.output === 'string'
		? [block.output]
		: (block.output ?? []).filter((item) => item.type === 'text').map((item) => item.text);
	for (const text of texts) {
		try {
			const value: unknown = JSON.parse(text);
			if (value && typeof value === 'object') return value as Record<string, unknown>;
		} catch {
			// 非 JSON Tool Result 不携带可信任务状态。
		}
	}
	return null;
}

function taskIdFromResult(block: ToolResultBlock): string | null {
	const taskId = resultPayload(block)?.task_id;
	return typeof taskId === 'string' && taskId.length > 0 && taskId.length <= 128
		? taskId
		: null;
}

/** 只检查受支持 Tool Result，绝不从用户文本识别 TASK_xxx。 */
export function findLatestInterpretationTaskId(messages: Msg[]): string | null {
	for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
		if (messages[messageIndex].role !== 'assistant') continue;
		const content = messages[messageIndex].content;
		for (let blockIndex = content.length - 1; blockIndex >= 0; blockIndex -= 1) {
			const block = content[blockIndex];
			if (block.type !== 'tool_result' || !TASK_TOOLS.has(block.name)) continue;
			const taskId = taskIdFromResult(block as ToolResultBlock);
			if (taskId) return taskId;
		}
	}
	return null;
}

/**
 * 最新任务 Tool Result 的刷新标识。同一 Task 创建新 Execution 时 task_id 不变，
 * 因此 Panel 需要用执行事实触发一次 Read API 刷新，之后仍由终态感知 polling 接管。
 */
export function findLatestInterpretationRefreshKey(messages: Msg[]): string | null {
	for (let messageIndex = messages.length - 1; messageIndex >= 0; messageIndex -= 1) {
		if (messages[messageIndex].role !== 'assistant') continue;
		const content = messages[messageIndex].content;
		for (let blockIndex = content.length - 1; blockIndex >= 0; blockIndex -= 1) {
			const block = content[blockIndex];
			if (block.type !== 'tool_result' || !TASK_TOOLS.has(block.name)) continue;
			const payload = resultPayload(block as ToolResultBlock);
			if (!payload || typeof payload.task_id !== 'string') continue;
			return JSON.stringify([
				payload.task_id,
				payload.execution_id,
				payload.command,
				payload.execution_status,
				payload.current_step,
				payload.report_ready,
			]);
		}
	}
	return null;
}

export function useInterpretationTask(
	agentId: string | null,
	sessionId: string | null,
	taskId: string | null,
) {
	return useQuery({
		queryKey: ['interpretation-task', agentId, sessionId, taskId],
		enabled: Boolean(agentId && sessionId && taskId),
		queryFn: ({ signal }) => interpretationApi.getTaskView(agentId!, sessionId!, taskId!, signal),
		refetchInterval: (query) => {
			const status = query.state.data?.current_execution?.execution_status;
			return status === 'QUEUED' || status === 'RUNNING' ? 1_000 : false;
		},
		retry: false,
	});
}

export function useInterpretationExecution(
	agentId: string | null,
	sessionId: string | null,
	taskId: string | null,
	executionId: string | null,
) {
	return useQuery({
		queryKey: ['interpretation-execution', agentId, sessionId, taskId, executionId],
		enabled: Boolean(agentId && sessionId && taskId && executionId),
		queryFn: ({ signal }) => interpretationApi.getExecutionView(
			agentId!, sessionId!, taskId!, executionId!, signal,
		),
		staleTime: Infinity,
		retry: false,
	});
}
