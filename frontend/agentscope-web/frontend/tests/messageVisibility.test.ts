import assert from 'node:assert/strict';
import test from 'node:test';

import type { Msg } from '@agentscope-ai/agentscope/message';

import {
	isInterpretationProgress,
	toUserVisibleChatMessage,
} from '../src/components/chat/messageVisibility.ts';

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

test('shows persisted business progress instead of a submission snapshot', () => {
	const original = message('assistant', [
		{ type: 'thinking', thinking: '开始解释井 WELL_MOCK_001\n▶ 数据解编开始' },
		{ type: 'tool_call', id: 'start', name: 'run_well_interpretation', input: '{}' },
		{ type: 'tool_result', id: 'start', name: 'run_well_interpretation', content: [] },
		{ type: 'text', text: '完整报告' },
	]);
	assert.equal(original.content.length, 4);
	const visible = toUserVisibleChatMessage(original);
	assert.deepEqual(visible?.content.map((block) => block.type), ['thinking', 'text']);
	assert.equal(original.content.length, 4);
	assert.ok(isInterpretationProgress(visible!.content[0]));
	assert.ok(isInterpretationProgress({ type: 'thinking', thinking: '\n开始重新解释：Execution #2' }));
	assert.ok(isInterpretationProgress({ type: 'thinking', thinking: '\n开始全量重新解释：Execution #3' }));
	assert.ok(isInterpretationProgress({ type: 'thinking', thinking: '\n▶ 数据解编开始\n▶ W01' }));
	assert.equal(isInterpretationProgress({ type: 'thinking', thinking: '普通模型思考' }), false);
});

test('keeps errors, legacy submissions and read-only tool results available', () => {
	const call = { type: 'tool_call' as const, id: 'start', name: 'run_well_interpretation', input: '{}' };
	assert.equal(toUserVisibleChatMessage(message('assistant', [call]))?.content.length, 1);
	const read = { ...call, name: 'get_interpretation_status' };
	const original = message('assistant', [
		{ type: 'thinking', thinking: '开始解释井 TEST' },
		read,
		{ type: 'text', text: '解释失败，请检查资料' },
	]);
	assert.equal(toUserVisibleChatMessage(original)?.content.length, 3);
});
