import { useQuery } from '@tanstack/react-query';

import { credentialApi, modelApi } from '@/api';
import type { CredentialView, ModelCard } from '@/api';

const BACKEND_MODEL_CREDENTIAL_ID = 'cnlc-backend-model';
const BACKEND_MODEL_NAME = 'qwen-plus';

export interface CredentialWithModels {
	credential: CredentialView;
	models: ModelCard[];
}

/**
 * 获取全部凭据及其可用模型，并按模型提供方类型分组。
 *
 * 后端专用凭据只暴露项目已配置的模型，避免用户在前端选择一个后端并未授权的模型。
 * 查询结果由 React Query 共享缓存；新增凭据后可通过 `refetch` 主动刷新。
 */
async function fetchGroups(): Promise<Record<string, CredentialWithModels[]>> {
	const { credentials } = await credentialApi.list();
	const result: Record<string, CredentialWithModels[]> = {};

	await Promise.all(
		credentials.map(async (credential) => {
			const type = credential.data.type as string | undefined;
			if (!type) return;
			if (!result[type]) result[type] = [];
			try {
				const { models } = await modelApi.list(type);
				// 普通凭据保留完整模型列表，项目内置凭据仅保留后端固定模型。
				const visibleModels =
					credential.id === BACKEND_MODEL_CREDENTIAL_ID
						? models.filter((model) => model.name === BACKEND_MODEL_NAME)
						: models;
				// Reverse-alphabetical, which is how the providers' naming
				// schemes rank themselves — gpt-5 before gpt-4, qwen3 before
				// qwen2 — so the strongest models sit at the top of the picker.
				result[type].push({
					credential,
					models: [...visibleModels].sort((a, b) =>
						b.name.localeCompare(a.name, undefined, { numeric: true }),
					),
				});
			} catch {
				result[type].push({ credential, models: [] });
			}
		}),
	);

	return result;
}

/** 分组模型列表的缓存键；凭据变化时由调用方用它使缓存失效。 */
export const AVAILABLE_MODELS_KEY = ['available-models'];

/** 返回当前可用模型分组及其加载、错误和刷新状态。 */
export function useAvailableModels() {
	const { data, isPending, error, refetch } = useQuery({
		queryKey: AVAILABLE_MODELS_KEY,
		queryFn: fetchGroups,
	});

	return {
		groups: data ?? {},
		loading: isPending,
		error: error as Error | null,
		refetch: () => void refetch(),
	};
}
