import { Download, Expand, ChartNoAxesCombined } from 'lucide-react';
import { useRef, useState } from 'react';

import {
	compositionBands,
	curveRange,
	curveSegments,
	depthRange,
	fraction,
} from './logPlotGeometry';
import type { LogPlotView } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';

// 固定图道宽度和线型只规定视觉，不代表专业标准刻度或解释判别规则。
type Track = {
	title: string;
	width: number;
	names?: string[];
	kind?: string;
	log?: boolean;
	special?: string;
};
const TRACKS: Track[] = [
	{ title: '分层', width: 28, special: 'formation' },
	{ title: '岩性指示', width: 94, names: ['SP', 'GR', 'CAL'], kind: 'raw' },
	{ title: '孔隙度', width: 94, names: ['CNL', 'DEN', 'AC'], kind: 'raw' },
	{
		title: '光电指数/全烃',
		width: 74,
		names: ['PE', 'PEF', 'TOTAL_HC', 'HEAVY_HC'],
		kind: 'raw',
	},
	{ title: '电阻率', width: 100, names: ['RT', 'RXO', 'RLLD', 'RLLS'], kind: 'raw', log: true },
	{ title: '取心', width: 23, special: 'core' },
	{ title: '岩性剖面', width: 37, special: 'lithology' },
	{
		title: '岩性剖面/组分',
		width: 104,
		names: ['SH', 'SAND', 'LIME', 'DOLO', 'VSH'],
		kind: 'interpreted',
		special: 'composition',
	},
	{ title: '孔隙度', width: 76, names: ['POR'], kind: 'interpreted' },
	{ title: '渗透率', width: 76, names: ['PERM'], kind: 'interpreted', log: true },
	{ title: '饱和度', width: 76, names: ['SW', 'SOG'], kind: 'interpreted' },
	{ title: '结论', width: 28, special: 'conclusion' },
];
const PALETTE: Record<string, string> = {
	SP: '#3338b2',
	GR: '#c12a2a',
	CAL: '#338341',
	CNL: '#36843b',
	DEN: '#9b266e',
	AC: '#373bac',
	RT: '#222222',
	RLLD: '#b72524',
	RLLS: '#338541',
	RXO: '#5c5caa',
	POR: '#a53273',
	PERM: '#893581',
	SW: '#b33938',
	SOG: '#3446b2',
	SH: '#777764',
	VSH: '#777764',
	SAND: '#e5b817',
	LIME: '#75a9c6',
	DOLO: '#b087bd',
	PE: '#bc8731',
	PEF: '#bc8731',
	TOTAL_HC: '#bc8731',
	HEAVY_HC: '#92943e',
};
const HEADER = 148,
	LEFT = 43,
	TOP = 4;
const fmt = (n: number) => Number(n.toPrecision(4)).toString();

/** 依据参考图的紧凑道头、密网格和细线排版；导出仍保存独立 SVG。 */
function LogSvg({ plot, height }: { plot: LogPlotView; height: number }) {
	const range = depthRange(plot);
	if (!range)
		return <div className="p-8 text-sm text-muted-foreground">暂无可绘制的深度数据</div>;
	const tracks = [...TRACKS];
	for (const curve of plot.curves) {
		if (!tracks.some((t) => t.kind === curve.kind && t.names?.includes(curve.name)))
			tracks.push({ title: curve.name, width: 76, names: [curve.name], kind: curve.kind });
	}
	const width = LEFT + tracks.reduce((sum, t) => sum + t.width, 0);
	const y = (depth: number) => HEADER + fraction(depth, range) * height;
	// 采用整洁刻度，避免短井段被有效位数舍入成重复深度标签。
	const rough = (range[1] - range[0]) / 12;
	const power = 10 ** Math.floor(Math.log10(rough));
	const major = ([1, 2, 5, 10].find((v) => v * power >= rough) ?? 10) * power;
	const minor = major / 5;
	const ticks = Array.from(
		{ length: Math.ceil((range[1] - range[0]) / minor) + 1 },
		(_, i) => Math.ceil(range[0] / minor - 1e-8) * minor + i * minor,
	).filter((d) => d <= range[1] + minor * 1e-7);
	const decimals = Math.max(0, -Math.floor(Math.log10(major)));
	let offset = LEFT;
	return (
		<svg
			xmlns="http://www.w3.org/2000/svg"
			viewBox={`0 0 ${width} ${height + HEADER + 22}`}
			width={width}
			height={height + HEADER + 22}
			role="img"
			aria-label={`${plot.well_name}最终解释曲线图`}
			style={{
				display: 'block',
				width: '100%',
				minWidth: 700,
				height: 'auto',
				background: '#fff',
				fontFamily: 'Arial, SimSun, serif',
				flexShrink: 0,
			}}
		>
			<title>{plot.well_name} · 最终解释曲线 · Mock</title>
			<rect width={width} height={height + HEADER + 22} fill="white" />
			<rect
				x={0.5}
				y={TOP}
				width={width - 1}
				height={HEADER + height - TOP}
				fill="none"
				stroke="#666"
				strokeWidth={0.65}
			/>
			<text x={LEFT / 2} y={18} fontSize={9} textAnchor="middle" fill="#222">
				深度
			</text>
			<text x={LEFT / 2} y={62} fontSize={9} textAnchor="middle" fill="#333">
				{plot.depth_reference}
			</text>
			<text x={LEFT / 2} y={76} fontSize={9} textAnchor="middle" fill="#333">
				({plot.depth_unit})
			</text>
			<text x={LEFT / 2} y={125} fontSize={8} textAnchor="middle" fill="#666">
				Mock
			</text>
			{ticks.map((d, i) => {
				const isMajor = Math.abs(d / major - Math.round(d / major)) < 1e-5;
				return (
					<g key={i}>
						<line
							x1={LEFT}
							x2={width}
							y1={y(d)}
							y2={y(d)}
							stroke={isMajor ? '#999' : '#dedede'}
							strokeWidth={isMajor ? 0.55 : 0.35}
						/>
						{isMajor && (
							<text
								x={LEFT - 4}
								y={y(d) + 3}
								textAnchor="end"
								fontSize={8}
								fill="#333"
							>
								{d.toFixed(decimals)}
							</text>
						)}
					</g>
				);
			})}
			{tracks.map((track, ti) => {
				const x = offset;
				offset += track.width;
				const w = track.width;
				// 按图道模板排列曲线，道头和曲线颜色始终一致。
				const curves = (track.names ?? []).flatMap((name) =>
					plot.curves.filter((c) => c.kind === track.kind && c.name === name),
				);
				const log = Boolean(track.log);
				const composition =
					track.special === 'composition' &&
					curves.length > 0 &&
					curves.every(
						(c) =>
							c.unit === 'fraction' &&
							c.values.every((v) => v === null || (v >= 0 && v <= 1)),
					);
				const titles = track.title.split('/');
				return (
					<g key={ti}>
						<rect
							x={x}
							y={TOP}
							width={w}
							height={HEADER + height - TOP}
							fill="none"
							stroke="#777"
							strokeWidth={0.6}
						/>
						<rect
							x={x + 0.3}
							y={TOP + 0.3}
							width={w - 0.6}
							height={29}
							fill="#fafafa"
							stroke="#aaa"
							strokeWidth={0.35}
						/>
						{titles.map((title, i) => (
							<text
								key={i}
								x={x + w / 2}
								y={titles.length === 1 ? 20 : 15 + i * 10}
								textAnchor="middle"
								fontSize={title.length > 3 && w < 50 ? 7 : 9}
								fill="#222"
							>
								{title}
							</text>
						))}
						{Array.from({ length: 9 }, (_, i) => (
							<line
								key={i}
								x1={x + ((i + 1) * w) / 10}
								x2={x + ((i + 1) * w) / 10}
								y1={HEADER}
								y2={HEADER + height}
								stroke={i === 4 ? '#bbb' : '#e1e1e1'}
								strokeWidth={i === 4 ? 0.5 : 0.3}
							/>
						))}
						<line
							x1={x}
							x2={x + w}
							y1={HEADER}
							y2={HEADER}
							stroke="#555"
							strokeWidth={0.7}
						/>
						{track.special === 'core' && (
							<text
								x={x + w / 2}
								y={65}
								textAnchor="middle"
								fontSize={9}
								fill="#777"
								style={{ writingMode: 'vertical-rl' }}
							>
								未提供
							</text>
						)}
						{track.special === 'conclusion' && (
							<text
								x={x + w / 2}
								y={65}
								textAnchor="middle"
								fontSize={9}
								fill="#222"
								style={{ writingMode: 'vertical-rl' }}
							>
								油气结论
							</text>
						)}
						{track.special === 'formation' && (
							<text
								x={x + w / 2}
								y={65}
								textAnchor="middle"
								fontSize={9}
								fill="#222"
								style={{ writingMode: 'vertical-rl' }}
							>
								地质分层
							</text>
						)}
						{track.names && curves.length === 0 && (
							<text
								x={x + w / 2}
								y={HEADER - 14}
								textAnchor="middle"
								fontSize={8}
								fill="#999"
							>
								未提供曲线
							</text>
						)}
						{composition &&
							compositionBands(curves.filter((c) => c.name !== 'VSH')).map(
								(band, bi) => (
									<polygon
										key={bi}
										points={band.points
											.map(([d, v]) => `${x + 1 + v * (w - 2)},${y(d)}`)
											.join(' ')}
										fill={PALETTE[band.name] ?? '#bbb'}
									>
										<title>{band.name} · fraction · 未提供的组分留白</title>
									</polygon>
								),
							)}
						{curves.map((curve, ci) => {
							const limits: [number, number] | null = composition
								? [0, 1]
								: curveRange(curve, log);
							const color = PALETTE[curve.name] ?? '#555';
							const rowY = 42 + ci * 22;
							const px = (v: number) => x + 1.5 + fraction(v, limits!, log) * (w - 3);
							return (
								<g key={curve.name}>
									<title>
										{curve.name} [{curve.unit}] · {curve.source}
									</title>
									{composition ? (
										<>
											<rect
												x={x + 1}
												y={rowY - 8}
												width={w - 2}
												height={23}
												fill={color}
											/>
											<text
												x={x + w / 2}
												y={rowY + 1}
												textAnchor="middle"
												fontSize={8}
												fill={curve.name === 'SAND' ? '#222' : '#fff'}
											>
												{curve.name} [{curve.unit}]
											</text>
											<text x={x + 3} y={rowY + 11} fontSize={7} fill="#222">
												0
											</text>
											<text
												x={x + w - 3}
												y={rowY + 11}
												textAnchor="end"
												fontSize={7}
												fill="#222"
											>
												1
											</text>
										</>
									) : (
										<>
											<text
												x={x + w / 2}
												y={rowY}
												textAnchor="middle"
												fontSize={8}
												fill={color}
											>
												{curve.name} [{curve.unit}]
											</text>
											<line
												x1={x + 3}
												x2={x + w - 3}
												y1={rowY + 4}
												y2={rowY + 4}
												stroke={color}
												strokeWidth={0.6}
												strokeDasharray={ci % 2 ? '3 2' : undefined}
											/>
											<text x={x + 3} y={rowY + 13} fontSize={7} fill={color}>
												{limits ? fmt(limits[0]) : '—'}
											</text>
											<text
												x={x + w - 3}
												y={rowY + 13}
												textAnchor="end"
												fontSize={7}
												fill={color}
											>
												{limits ? fmt(limits[1]) : '—'}
											</text>
										</>
									)}
									{limits &&
										curveSegments(curve, log).map((segment, si) => (
											<g key={si}>
												<polyline
													fill="none"
													stroke={color}
													strokeWidth={0.75}
													strokeDasharray={
														!composition && ci % 2 ? '3 1.5' : undefined
													}
													points={segment
														.map(([d, v]) => `${px(v)},${y(d)}`)
														.join(' ')}
												/>
												{segment.map(([d, v], pi) => (
													<circle
														key={pi}
														cx={px(v)}
														cy={y(d)}
														r={segment.length === 1 ? 1.8 : 2.2}
														fill={
															segment.length === 1
																? color
																: 'transparent'
														}
													>
														<title>
															{curve.name}: {v} {curve.unit} · {d}{' '}
															{plot.depth_unit} · {curve.source}
														</title>
													</circle>
												))}
											</g>
										))}
								</g>
							);
						})}
						{['formation', 'lithology', 'conclusion'].includes(track.special ?? '') &&
							plot.intervals.map((interval, ii) => {
								const label =
									track.special === 'formation'
										? [interval.formation, interval.layer_no]
												.filter(Boolean)
												.join(' / ')
										: track.special === 'lithology'
											? interval.lithology
											: interval.label;
								if (!label) return null;
								const mid = (y(interval.top) + y(interval.bottom)) / 2;
								return (
									<g key={ii}>
										<rect
											x={x + (track.special === 'conclusion' ? 6 : 1)}
											y={y(interval.top)}
											width={w - (track.special === 'conclusion' ? 12 : 2)}
											height={y(interval.bottom) - y(interval.top)}
											fill={
												track.special === 'conclusion'
													? '#2424db'
													: '#e8e8df'
											}
											stroke="#666"
											strokeWidth={0.45}
										>
											<title>
												{label} · {interval.top}–{interval.bottom} m ·{' '}
												{interval.source}
											</title>
										</rect>
										<text
											x={x + w / 2}
											y={mid}
											textAnchor="middle"
											dominantBaseline="middle"
											fontSize={8}
											fill={track.special === 'conclusion' ? '#fff' : '#333'}
											style={{ writingMode: 'vertical-rl' }}
										>
											{label.replace('（预设）', '')}
										</text>
									</g>
								);
							})}
					</g>
				);
			})}
			<text x={4} y={HEADER + height + 14} fontSize={8} fill="#666">
				{plot.well_name} · Mock ·
				刻度按数据范围；缺值断线；对数轴不绘非正值；颜色不代表专业判别规则。
			</text>
		</svg>
	);
}

/** 绑定执行版本的多道图；放大和导出都使用同一份数据。 */
export function WellLogPlot({
	plot,
	executionId,
}: {
	plot: LogPlotView | null | undefined;
	executionId: string;
}) {
	const [expanded, setExpanded] = useState(false);
	const [height, setHeight] = useState(800);
	const container = useRef<HTMLDivElement>(null);
	if (!plot) return <div className="text-xs text-muted-foreground">尚无曲线数据</div>;
	const exportSvg = () => {
		const svg = container.current?.querySelector('svg');
		if (!svg) return;
		const url = URL.createObjectURL(
			new Blob([new XMLSerializer().serializeToString(svg)], {
				type: 'image/svg+xml;charset=utf-8',
			}),
		);
		const anchor = document.createElement('a');
		anchor.href = url;
		anchor.download = `well-log-${executionId}.svg`;
		anchor.click();
		setTimeout(() => URL.revokeObjectURL(url), 1000);
	};
	return (
		<section className="space-y-2 rounded-lg border p-3" aria-label="最终解释曲线">
			<div className="flex flex-wrap items-center justify-between gap-2">
				<div className="flex items-center gap-2 font-medium">
					<ChartNoAxesCombined className="size-4" />
					最终解释曲线
				</div>
				<div className="flex items-center gap-1">
					<select
						aria-label="曲线纵向比例"
						className="rounded border bg-background p-1 text-xs"
						value={height}
						onChange={(e) => setHeight(Number(e.target.value))}
					>
						<option value={800}>标准</option>
						<option value={1600}>放大 2 倍</option>
						<option value={2400}>放大 3 倍</option>
					</select>
					<Button size="sm" variant="outline" onClick={() => setExpanded(true)}>
						<Expand className="size-3" />
						全屏
					</Button>
					<Button
						size="sm"
						variant="outline"
						onClick={exportSvg}
						disabled={!depthRange(plot)}
					>
						<Download className="size-3" />
						导出 SVG
					</Button>
				</div>
			</div>
			<div className="text-xs text-muted-foreground">
				{plot.well_name} · {plot.curves.length} 条曲线 · {plot.intervals.length} 个层段 ·
				可横纵滚动，悬停采样点查看数值
			</div>
			<div ref={container} className="max-h-[560px] overflow-auto rounded border bg-white">
				<LogSvg plot={plot} height={height} />
			</div>
			{plot.warnings.map((warning, i) => (
				<p key={i} className="text-xs text-amber-700 dark:text-amber-300">
					{warning}
				</p>
			))}
			<Dialog open={expanded} onOpenChange={setExpanded}>
				<DialogContent className="h-[95vh] w-[96vw] max-w-none sm:max-w-none flex flex-col">
					<DialogTitle>最终解释曲线 · {plot.well_name}</DialogTitle>
					<DialogDescription>
						Mock 演示 · 按深度对齐 · 使用滚动条查看全部图道和井段
					</DialogDescription>
					<div className="min-h-0 flex-1 overflow-auto border bg-white">
						<LogSvg plot={plot} height={height} />
					</div>
					<Button variant="outline" onClick={exportSvg}>
						<Download className="size-4" />
						导出当前版本 SVG
					</Button>
				</DialogContent>
			</Dialog>
		</section>
	);
}
