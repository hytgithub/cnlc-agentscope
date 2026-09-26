import { client } from './client';
import type { InterpretationExecutionView, InterpretationTaskView } from './types';

const taskPath = (agentId: string, sessionId: string, taskId: string) =>
	`/cnlc/interpretation/agents/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/tasks/${encodeURIComponent(taskId)}`;

/** 只读解释任务 API；所有路径都携带 AgentScope 会话身份。 */
export const interpretationApi = {
	getTaskView: (
		agentId: string,
		sessionId: string,
		taskId: string,
		signal?: AbortSignal,
	) => client.get<InterpretationTaskView>(taskPath(agentId, sessionId, taskId), undefined, {
		silent: true,
		signal,
	}),
	getExecutionView: (
		agentId: string,
		sessionId: string,
		taskId: string,
		executionId: string,
		signal?: AbortSignal,
	) => client.get<InterpretationExecutionView>(
		`${taskPath(agentId, sessionId, taskId)}/executions/${encodeURIComponent(executionId)}`,
		undefined,
		{ silent: true, signal },
	),
};
