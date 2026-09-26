import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

interface HintSource {
	label?: string;
	sublabel?: string;
}

const INTERPRETATION_WRITE_TOOLS = new Set([
	'run_well_interpretation',
	'modify_well_interpretation',
	'rerun_well_interpretation',
]);

/** 识别后端固定的业务过程开场；普通模型思考仍使用原展示方式。 */
export function isInterpretationProgress(block: ContentBlock): boolean {
	if (block.type !== 'thinking') return false;
	const text = block.thinking.trimStart();
	return [
		'开始解释井 ',
		'开始重新解释：',
		'开始全量重新解释：',
		'▶ 数据解编开始',
	].some((prefix) => text.startsWith(prefix));
}

/** AgentScope 运行上下文使用带结构化 source 的 HintBlock 注入助手消息。 */
function isInternalRuntimeHint(block: ContentBlock): boolean {
	if (block.type !== 'hint' || !block.source) return false;
	try {
		const source = JSON.parse(block.source) as HintSource;
		return source.label === 'System' && source.sublabel === 'Runtime State';
	} catch {
		return false;
	}
}

/**
 * 只在展示边界移除内部消息。原始 Session/context 保持不变；包含内部 hint 的
 * AssistantMsg 保留业务过程和正文；有过程时省略重复的写工具提交快照。
 */
export function toUserVisibleChatMessage(message: Msg): Msg | null {
	if (message.role === 'system') return null;
	if (message.role !== 'assistant') return message;

	const hasProgress = message.content.some(isInterpretationProgress);
	// 有完整过程时，提交时的 QUEUED 快照不再占据聊天区，也不冒充当前状态。
	// 无过程的旧消息或错误回复仍保留工具结果；持久化内容和右侧版本面板不变。
	const content = message.content.filter((block) => {
		if (isInternalRuntimeHint(block)) return false;
		return !(
			hasProgress &&
			(block.type === 'tool_call' || block.type === 'tool_result') &&
			INTERPRETATION_WRITE_TOOLS.has(block.name)
		);
	});
	if (content.length === 0) return null;
	return content.length === message.content.length
		? message
		: { ...message, content };
}
