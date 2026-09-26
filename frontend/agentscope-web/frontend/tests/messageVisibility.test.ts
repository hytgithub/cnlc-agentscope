import assert from 'node:assert/strict';
import test from 'node:test';

import type { Msg } from '@agentscope-ai/agentscope/message';

import { toUserVisibleChatMessage } from '../src/components/chat/messageVisibility.ts';

function message(role: Msg['role'], content: Msg['content']): Msg {
	return { id: role, name: role, role, content } as Msg;
}

test('hides AgentScope runtime context by structured hint source', () => {
	const internalHint = {
		type: 'hint' as const,
		hint: 'Treat the following as the ground truth at this point of the conversation...',
		source: JSON.stringify({ label: 'System', sublabel: 'Runtime State' }),
	};
	const visible = toUserVisibleChatMessage(
		message('assistant', [internalHint, { type: 'text', text: '正常回答' }]),
	);
	assert.equal(visible?.content.length, 1);
	assert.equal(visible?.content[0].type, 'text');
	assert.equal(toUserVisibleChatMessage(message('assistant', [internalHint])), null);
});

test('keeps user, ordinary hints and assistant presentation blocks visible', () => {
	assert.ok(toUserVisibleChatMessage(message('user', [{ type: 'text', text: '输入' }])));
	const assistant = message('assistant', [
		{ type: 'hint', hint: '需要确认', source: 'Permission' },
		{ type: 'thinking', thinking: '过程' },
		{ type: 'text', text: '结果' },
	]);
	assert.equal(toUserVisibleChatMessage(assistant)?.content.length, 3);
});

test('hides system-role messages without changing persisted data', () => {
	const system = message('system', [{ type: 'text', text: '内部系统提示' }]);
	assert.equal(toUserVisibleChatMessage(system), null);
	assert.equal(system.content.length, 1);
});
