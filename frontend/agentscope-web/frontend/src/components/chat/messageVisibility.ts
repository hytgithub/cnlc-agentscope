import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

interface HintSource {
	label?: string;
	sublabel?: string;
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
 * AssistantMsg 仍保留其中正常的 Thinking、ToolCall、ToolResult 和 Text。
 */
export function toUserVisibleChatMessage(message: Msg): Msg | null {
	if (message.role === 'system') return null;
	if (message.role !== 'assistant') return message;

	const content = message.content.filter((block) => !isInternalRuntimeHint(block));
	if (content.length === 0) return null;
	return content.length === message.content.length
		? message
		: { ...message, content };
}
