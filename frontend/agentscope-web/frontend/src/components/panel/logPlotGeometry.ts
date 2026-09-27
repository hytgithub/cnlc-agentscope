import type { LogPlotCurve, LogPlotView } from '../../api/types.ts';

/** 所有图道使用一个向下增大的深度轴，单点也保留而非复制为连续曲线。 */
export function depthRange(plot: LogPlotView): [number, number] | null {
	const depths = plot.curves
		.flatMap((c) => c.depths)
		.concat(plot.intervals.flatMap((i) => [i.top, i.bottom]));
	let min = Infinity,
		max = -Infinity;
	for (const depth of depths)
		if (Number.isFinite(depth)) {
			min = Math.min(min, depth);
			max = Math.max(max, depth);
		}
	if (!Number.isFinite(min)) return null;
	return min === max ? [min - 0.5, max + 0.5] : [min, max];
}

export function curveRange(curve: LogPlotCurve, logarithmic: boolean): [number, number] | null {
	let min = Infinity,
		max = -Infinity;
	for (const value of curve.values) {
		if (value === null || !Number.isFinite(value) || (logarithmic && value <= 0)) continue;
		min = Math.min(min, value);
		max = Math.max(max, value);
	}
	if (!Number.isFinite(min)) return null;
	if (min === max)
		return logarithmic
			? [min / 2, max * 2]
			: [min - (Math.abs(min) * 0.1 || 1), max + (Math.abs(max) * 0.1 || 1)];
	return [min, max];
}

/** 空值或对数轴非正值断开路径，绝不跨缺失段连线。 */
export function curveSegments(curve: LogPlotCurve, logarithmic: boolean): [number, number][][] {
	const segments: [number, number][][] = [];
	let current: [number, number][] = [];
	curve.depths.forEach((depth, index) => {
		const value = curve.values[index];
		if (
			value === null ||
			value === undefined ||
			!Number.isFinite(value) ||
			!Number.isFinite(depth) ||
			(logarithmic && value <= 0)
		) {
			if (current.length) segments.push(current);
			current = [];
		} else current.push([depth, value]);
	});
	if (current.length) segments.push(current);
	return segments;
}

export function fraction(value: number, range: [number, number], logarithmic = false): number {
	const scale = (n: number) => (logarithmic ? Math.log10(n) : n);
	return (scale(value) - scale(range[0])) / (scale(range[1]) - scale(range[0]));
}

/** 仅相同深度网格、显式 fraction 组分可堆叠；未知剩余量留白，非法点断开。 */
export function compositionBands(
	curves: LogPlotCurve[],
): { name: string; points: [number, number][] }[] {
	if (
		!curves.length ||
		curves.some(
			(c) =>
				c.unit !== 'fraction' ||
				c.depths.length !== curves[0].depths.length ||
				c.depths.some((d, i) => d !== curves[0].depths[i]),
		)
	)
		return [];
	const bands: { name: string; points: [number, number][] }[] = [];
	for (let i = 0; i < curves[0].depths.length - 1; i++) {
		const pairs = curves.map((c) => [c.values[i], c.values[i + 1]]);
		if (pairs.some((p) => p.some((v) => v === null || !Number.isFinite(v) || v! < 0 || v! > 1)))
			continue;
		if ([0, 1].some((j) => pairs.reduce((sum, p) => sum + p[j]!, 0) > 1.000001)) continue;
		let left = 0,
			right = 0;
		pairs.forEach((pair, j) => {
			const top = curves[0].depths[i],
				bottom = curves[0].depths[i + 1];
			bands.push({
				name: curves[j].name,
				points: [
					[top, left],
					[top, left + pair[0]!],
					[bottom, right + pair[1]!],
					[bottom, right],
				],
			});
			left += pair[0]!;
			right += pair[1]!;
		});
	}
	return bands;
}
