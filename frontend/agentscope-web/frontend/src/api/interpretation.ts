import { client } from './client';
import type { InterpretationExecutionView, InterpretationTaskView, StageProgress } from './types';

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
	getStageProgress: (
		agentId: string,
		sessionId: string,
		taskId: string,
		executionId: string,
		signal?: AbortSignal,
	) => client.get<StageProgress>(
		`${taskPath(agentId, sessionId, taskId)}/executions/${encodeURIComponent(executionId)}/stage-progress`,
		undefined,
		{ silent: true, signal },
	),
	confirmStage: (
		agentId: string,
		sessionId: string,
		taskId: string,
		executionId: string,
		stage: StageProgress['current_stage'],
		expectedStageRunId: string,
	) => client.post<StageProgress>(
		`${taskPath(agentId, sessionId, taskId)}/executions/${encodeURIComponent(executionId)}/stages/${encodeURIComponent(stage ?? '')}/confirm`,
		{ expected_stage_run_id: expectedStageRunId },
		undefined,
		{ silent: true },
	),
};
