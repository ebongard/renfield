/**
 * ChartArtifact — hand-rolled bar/line SVG from typed series (NO charting dep).
 *
 * The SVG markup is OURS, emitted from validated numeric series — never model
 * SVG, so the SVG injection surface (`<script>`, `onload`, `<foreignObject>`)
 * does not exist here. The chart scales to its container via `viewBox`, so it
 * never overflows the bubble (§9). Coordinates are zod-finite (no NaN/Infinity),
 * so the viewBox can't be blown.
 *
 * a11y / colorblind (§9, WCAG 1.4.1): series are differentiated by color PLUS a
 * non-color channel — direct end-of-line labels for line charts and per-series
 * fill patterns + a labelled legend for bars — so the chart is readable in
 * grayscale / for CVD users. The SVG carries role="img" + a <title>/aria-label
 * summary. Colors come from the accent + primary axes (no new hues).
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import type { ChartData, ChartSeries } from './artifactSchema';

// Data palette drawn ONLY from the DESIGN.md accent (turquoise) + primary
// (crimson) axes — no new hues. Cycled if there are more series than colors;
// the non-color channel (label/pattern) carries the real disambiguation.
const SERIES_COLORS = [
  '#00ba98', // accent-600
  '#a5162f', // primary-700
  '#00937c', // accent-700
  '#e63e54', // primary-500
  '#005f53', // accent-900
  '#f7a4ae', // primary-300
];
// SVG <pattern> ids per series for the bar non-color channel (solid + hatches).
const BAR_PATTERNS = ['solid', 'diagonal', 'dots', 'cross', 'horizontal', 'vertical'] as const;

const VIEW_W = 480;
const VIEW_H = 240;
const PAD = { top: 16, right: 72, bottom: 28, left: 40 };

interface Bounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

function computeBounds(series: ChartSeries[]): Bounds | null {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  let any = false;
  for (const s of series) {
    for (const p of s.points) {
      any = true;
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
  }
  if (!any) return null;
  // Always include 0 on the y-axis so bar heights are honest.
  if (minY > 0) minY = 0;
  if (maxY < 0) maxY = 0;
  if (minX === maxX) {
    minX -= 0.5;
    maxX += 0.5;
  }
  if (minY === maxY) {
    maxY += 1;
  }
  return { minX, maxX, minY, maxY };
}

export default function ChartArtifact({ data, title }: { data: ChartData; title?: string }) {
  const { t } = useTranslation();
  const idPrefix = useId();
  const bounds = computeBounds(data.series);

  if (!bounds) {
    return (
      <p className="text-sm italic text-accent-700 dark:text-accent-300">
        {t('chat.artifacts.emptyChart')}
      </p>
    );
  }

  const plotW = VIEW_W - PAD.left - PAD.right;
  const plotH = VIEW_H - PAD.top - PAD.bottom;
  const sx = (x: number) => PAD.left + ((x - bounds.minX) / (bounds.maxX - bounds.minX)) * plotW;
  const sy = (y: number) => PAD.top + plotH - ((y - bounds.minY) / (bounds.maxY - bounds.minY)) * plotH;
  const yZero = sy(0);

  const showLegend = data.series.length > 1;
  const summary =
    title ||
    t('chat.artifacts.chartSummary', {
      type: t(`chat.artifacts.chartType.${data.chartType}`),
      count: data.series.length,
    });

  return (
    <div className="max-w-full">
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        className="w-full h-auto"
        role="img"
        aria-label={summary}
        preserveAspectRatio="xMidYMid meet"
      >
        <title>{summary}</title>
        {data.chartType === 'bar' && (
          <defs>
            {data.series.map((_, si) => (
              <BarPattern
                key={si}
                id={`${idPrefix}-pat-${si}`}
                kind={BAR_PATTERNS[si % BAR_PATTERNS.length]}
                color={SERIES_COLORS[si % SERIES_COLORS.length]}
              />
            ))}
          </defs>
        )}

        {/* x-axis baseline at y=0 */}
        <line
          x1={PAD.left}
          y1={yZero}
          x2={VIEW_W - PAD.right}
          y2={yZero}
          className="stroke-gray-300 dark:stroke-gray-600"
          strokeWidth={1}
        />

        {data.chartType === 'bar'
          ? renderBars(data.series, sx, sy, yZero, plotW, bounds, idPrefix)
          : renderLines(data.series, sx, sy)}
      </svg>

      {showLegend && (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          {data.series.map((s, si) => (
            <li key={si} className="flex items-center gap-1.5 text-gray-700 dark:text-gray-300">
              <span
                aria-hidden="true"
                className="inline-block w-3 h-3 rounded-sm"
                style={{ backgroundColor: SERIES_COLORS[si % SERIES_COLORS.length] }}
              />
              <span>{s.label || t('chat.artifacts.seriesFallback', { n: si + 1 })}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function renderBars(
  series: ChartSeries[],
  sx: (x: number) => number,
  sy: (y: number) => number,
  yZero: number,
  plotW: number,
  bounds: Bounds,
  idPrefix: string,
) {
  // Group bars per x position; bar width derives from the densest series.
  const maxPoints = Math.max(1, ...series.map((s) => s.points.length));
  const slot = plotW / (maxPoints + 1);
  const groupW = slot * 0.7;
  const barW = groupW / series.length;

  return series.flatMap((s, si) =>
    s.points.map((p, pi) => {
      const cx = sx(p.x);
      const x = cx - groupW / 2 + si * barW;
      const yTop = sy(Math.max(p.y, 0));
      const yBot = sy(Math.min(p.y, 0));
      const h = Math.max(0, yBot - yTop);
      return (
        <rect
          key={`${si}-${pi}`}
          x={x}
          y={yTop}
          width={Math.max(1, barW - 1)}
          height={h}
          fill={`url(#${idPrefix}-pat-${si})`}
          stroke={SERIES_COLORS[si % SERIES_COLORS.length]}
          strokeWidth={0.5}
        />
      );
    }),
  );
}

function renderLines(
  series: ChartSeries[],
  sx: (x: number) => number,
  sy: (y: number) => number,
) {
  return series.map((s, si) => {
    const color = SERIES_COLORS[si % SERIES_COLORS.length];
    const pts = s.points.map((p) => ({ px: sx(p.x), py: sy(p.y) }));
    const d = pts.map((pt, i) => `${i === 0 ? 'M' : 'L'}${pt.px.toFixed(2)},${pt.py.toFixed(2)}`).join(' ');
    const last = pts[pts.length - 1];
    return (
      <g key={si}>
        <path d={d} fill="none" stroke={color} strokeWidth={2} />
        {pts.map((pt, i) => (
          <circle key={i} cx={pt.px} cy={pt.py} r={2.5} fill={color} />
        ))}
        {/* Direct end-of-line label = the non-color channel (CVD-safe). */}
        {last && s.label && (
          <text
            x={last.px + 4}
            y={last.py}
            dy="0.32em"
            className="fill-gray-700 dark:fill-gray-300"
            fontSize={10}
          >
            {s.label}
          </text>
        )}
      </g>
    );
  });
}

/** Per-series SVG fill pattern — the bar non-color disambiguation channel. */
function BarPattern({ id, kind, color }: { id: string; kind: string; color: string }) {
  if (kind === 'solid') {
    return (
      <pattern id={id} width={1} height={1} patternUnits="objectBoundingBox">
        <rect width={1} height={1} fill={color} />
      </pattern>
    );
  }
  const strokeProps = { stroke: color, strokeWidth: 1.4 };
  return (
    <pattern id={id} width={6} height={6} patternUnits="userSpaceOnUse">
      <rect width={6} height={6} fill={color} fillOpacity={0.25} />
      {kind === 'diagonal' && <path d="M0,6 L6,0" {...strokeProps} />}
      {kind === 'horizontal' && <path d="M0,3 L6,3" {...strokeProps} />}
      {kind === 'vertical' && <path d="M3,0 L3,6" {...strokeProps} />}
      {kind === 'cross' && (
        <>
          <path d="M0,6 L6,0" {...strokeProps} />
          <path d="M0,0 L6,6" {...strokeProps} />
        </>
      )}
      {kind === 'dots' && <circle cx={3} cy={3} r={1.4} fill={color} />}
    </pattern>
  );
}
