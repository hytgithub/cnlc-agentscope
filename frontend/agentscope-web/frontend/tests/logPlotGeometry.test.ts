import assert from 'node:assert/strict';
import test from 'node:test';

import {
	compositionBands,
	curveRange,
	curveSegments,
	depthRange,
	fraction,
} from '../src/components/panel/logPlotGeometry.ts';

const curve = {
	name: 'RT',
	unit: 'ohm.m',
	depths: [1, 2, 3, 4, 5],
	values: [10, null, 0, 100, 1000],
	source: 'test',
	is_mock: true,
	kind: 'raw' as const,
};

test('gaps and nonpositive logarithmic values break paths without removing valid single points', () => {
	assert.deepEqual(curveSegments(curve, true), [
		[[1, 10]],
		[
			[4, 100],
			[5, 1000],
		],
	]);
	assert.deepEqual(curveRange(curve, true), [10, 1000]);
	assert.equal(fraction(100, [10, 1000], true), 0.5);
});

test('composition preserves missing remainder and refuses mismatched grids or invalid totals', () => {
	const a = { ...curve, name: 'SH', unit: 'fraction', depths: [1, 2], values: [0.2, 0.3] };
	const b = { ...a, name: 'SAND', values: [0.5, 0.5] };
	assert.deepEqual(compositionBands([a, b])[1].points, [
		[1, 0.2],
		[1, 0.7],
		[2, 0.8],
		[2, 0.3],
	]);
	assert.deepEqual(compositionBands([a, { ...b, depths: [1, 3] }]), []);
	assert.deepEqual(compositionBands([a, { ...b, values: [0.9, 0.9] }]), []);
	assert.deepEqual(compositionBands([{ ...a, values: [null, 0.3] }]), []);
});

test('single point gets a display extent and depth increases downward', () => {
	const plot = {
		well_name: 'test',
		depth_unit: 'm',
		depth_reference: 'MD',
		curves: [{ ...curve, depths: [20], values: [5] }],
		intervals: [],
		warnings: [],
	};
	assert.deepEqual(depthRange(plot), [19.5, 20.5]);
	assert.equal(fraction(20, [19.5, 20.5]), 0.5);
	assert.deepEqual(curveRange(plot.curves[0], false), [4.5, 5.5]);
	assert.equal(depthRange({ ...plot, curves: [] }), null);
});
