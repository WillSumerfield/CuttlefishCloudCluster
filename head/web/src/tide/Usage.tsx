import { useMemo, useState } from "react";
import { edgeFade } from "../app/edgeFade";
import { Tip } from "../app/Tip";
import { pigment } from "../karma/skin";
import type { UsageUser } from "../api";

/**
 * Who spent the cluster, over the last month. The warm counter-tide to the horizon above: this is PEOPLE,
 * so it wears pigment (umber debtor → gold creditor), never iridescence. A stacked area — total load and
 * each User's share of it at once. Hover a band to solo it.
 *
 * Measured in Karma spent, not GPU-hours: a settle row already weights an hour by the card's Power and
 * the Demand it ran against, and raw hours would call an hour on the quiet little card equal to an hour
 * on the coveted one — the exact distinction the page above exists to draw.
 */

// FOOT — the bottom band runs this far past the plot so edgeFade dissolves filled area, not a hard
// baseline (the SVG grows to match, so the plot keeps its height and the foot melts into the gap above
// the legend). Mirrors TideHorizon.footDrop.
const VB_W = 640, VB_H = 200, PAD_T = 12, PAD_B = 6, FOOT = 50;

const rgb = (c: [number, number, number]) =>
  `rgb(${(c[0] * 255) | 0}, ${(c[1] * 255) | 0}, ${(c[2] * 255) | 0})`;

type Pt = { x: number; y: number };

// Quadratic-through-midpoints smoothing — the same curve TideHorizon traces, as an SVG segment string
// (no leading M): control points are the data points, segment ends are the midpoints between them. Two
// bands share a boundary, and smoothing the same points forward vs reversed yields the same curve, so
// the stack still tiles without gaps.
const smooth = (p: Pt[]): string => {
  if (p.length < 2) return "";
  if (p.length === 2) return `L${p[1].x},${p[1].y}`;
  const mid = (a: Pt, b: Pt) => `${(a.x + b.x) / 2},${(a.y + b.y) / 2}`;
  let d = `Q${p[1].x},${p[1].y} ${mid(p[1], p[2])}`;
  for (let i = 2; i < p.length - 2; i++) d += `Q${p[i].x},${p[i].y} ${mid(p[i], p[i + 1])}`;
  d += `Q${p[p.length - 2].x},${p[p.length - 2].y} ${p[p.length - 1].x},${p[p.length - 1].y}`;
  return d;
};

export function Usage({ data }: { data: UsageUser[] }) {
  const [hover, setHover] = useState(-1);

  const { bands, totals, grand } = useMemo(() => {
    const days = data[0]?.series.length ?? 0;
    // Total load per day, for the y-scale and each User's share.
    const dayTotal = Array.from({ length: days }, (_, d) => data.reduce((n, u) => n + u.series[d], 0));
    const maxTotal = Math.max(1, ...dayTotal);
    const x = (d: number) => (d / Math.max(1, days - 1)) * VB_W;
    const y = (v: number) => PAD_T + (VB_H - PAD_T - PAD_B) * (1 - v / maxTotal);

    // Stack bottom→up in array order (biggest User first, so it anchors the base).
    const lower = Array(days).fill(0);
    const bands = data.map((u, i) => {
      const upper = lower.map((lo, d) => lo + u.series[d]);
      const top = upper.map((v, d) => ({ x: x(d), y: y(v) }));
      // The base band (i=0) drops to the foot; interior bands close on the band below them.
      const bot = lower.map((v, d) => ({ x: x(d), y: i === 0 ? VB_H + FOOT : y(v) })).reverse();
      const path = `M${top[0].x},${top[0].y}${smooth(top)}L${bot[0].x},${bot[0].y}${smooth(bot)}Z`;
      for (let d = 0; d < days; d++) lower[d] = upper[d];
      return { name: u.username, color: rgb(pigment(u.balance, 0.5)), path };
    });

    const totals = data.map((u) => u.series.reduce((n, v) => n + v, 0));
    const grand = totals.reduce((n, v) => n + v, 0) || 1;
    return { bands, totals, grand };
  }, [data]);

  return (
    <div>
      <svg viewBox={`0 0 ${VB_W} ${VB_H + FOOT}`} preserveAspectRatio="none" className="block h-[13.75rem] w-full md:h-[16.25rem]" style={edgeFade()}>
        {bands.map((b, i) => (
          <path
            key={b.name}
            d={b.path}
            fill={b.color}
            fillOpacity={hover < 0 ? 0.82 : hover === i ? 0.98 : 0.22}
            style={{ transition: "fill-opacity 160ms" }}
            onPointerEnter={() => setHover(i)}
            onPointerLeave={() => setHover(-1)}
          />
        ))}
      </svg>

      {/* Legend doubles as the readout — each User, their pigment, their share; the hovered band bolds. */}
        <div className="mt-5 mb-4 flex items-center justify-between gap-4">
          <ul className="flex flex-wrap gap-x-6 gap-y-2">
            {bands.map((b, i) => (
              <li
                key={b.name}
                className="flex items-center gap-2 transition-opacity"
                style={{ opacity: hover < 0 || hover === i ? 1 : 0.4 }}
                onPointerEnter={() => setHover(i)}
                onPointerLeave={() => setHover(-1)}
              >
                <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: b.color }} />
                <span className="text-[13px]" style={{ color: hover === i ? "var(--ink)" : "var(--ink-soft)" }}>
                  {b.name}
                </span>
                <span className="num text-[12px] text-ink-mute">{Math.round((totals[i] / grand) * 100)}%</span>
              </li>
            ))}
          </ul>    
          <Tip className="label shrink-0 text-right" text="Karma spent per person over the last 30 days. Measured in Karma rather than hours because an hour on the coveted card is not an hour on the quiet one.">Usage · last 30 days</Tip>
        </div>
    </div>
  );
}
