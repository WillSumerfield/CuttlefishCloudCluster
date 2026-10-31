import { useEffect, useId, useMemo, useRef, useState } from "react";
import { edgeFade } from "../app/edgeFade";
import { Tip } from "../app/Tip";
import { pigment } from "../karma/skin";
import type { UsageUser } from "../api";
import { LEGEND_W, SEAM, SEAM_FADE, SEAM_FONT, SEAM_TICKS } from "./seam";

/**
 * Who spent the cluster, over the last month. The warm counter-tide to the horizon above: this is PEOPLE,
 * so it wears pigment (umber debtor → gold creditor), never iridescence. A stacked area, hung UPSIDE DOWN
 * from the seam with the Tide (see seam.ts): the base is at the top, solid where it meets the water, and
 * the bands' waves reach down the page, fading as they go. Hover a band to solo it.
 *
 * Measured in Karma spent, not GPU-hours: a settle row already weights an hour by the card's Power and
 * the Demand it ran against, and raw hours would call an hour on the quiet little card equal to an hour
 * on the coveted one — the exact distinction the page above exists to draw.
 *
 * Drawn in real pixels (viewBox = measured box), not a stretched viewBox: the dates are text cut out of
 * the bands by an SVG mask, and a non-uniform stretch would distort them.
 */

// FOOT — the solid run from the seam to the plot's base: room for the overlap and the knocked-out dates.
// PAD_OUT — space past the tallest day at the far (bottom) end.
const FOOT = 80, PAD_OUT = 12;
// Warm light over the seam, so even an umber (in-debt) base band gives the dates contrast.
const SEAM_WASH = "rgba(255,226,165,0.4)";

const rgb = (c: [number, number, number]) =>
  `rgb(${(c[0] * 255) | 0}, ${(c[1] * 255) | 0}, ${(c[2] * 255) | 0})`;

type Pt = { x: number; y: number };

// Quadratic-through-midpoints smoothing — the same curve TideHorizon traces, as an SVG segment string
// (no leading M). Two bands share a boundary, and smoothing the same points forward vs reversed yields the
// same curve, so the stack still tiles without gaps.
const smooth = (p: Pt[]): string => {
  if (p.length < 2) return "";
  if (p.length === 2) return `L${p[1].x},${p[1].y}`;
  const mid = (a: Pt, b: Pt) => `${(a.x + b.x) / 2},${(a.y + b.y) / 2}`;
  let d = `Q${p[1].x},${p[1].y} ${mid(p[1], p[2])}`;
  for (let i = 2; i < p.length - 2; i++) d += `Q${p[i].x},${p[i].y} ${mid(p[i], p[i + 1])}`;
  d += `Q${p[p.length - 2].x},${p[p.length - 2].y} ${p[p.length - 1].x},${p[p.length - 1].y}`;
  return d;
};

const dayLabel = (daysAgo: number) =>
  new Date(Date.now() - daysAgo * 86400e3).toLocaleDateString(undefined, { month: "short", day: "numeric" });

export function Usage({ data }: { data: UsageUser[] }) {
  const [hover, setHover] = useState(-1);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ w: 0, h: 0 });
  const id = useId();

  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setBox({ w: Math.floor(r.width), h: Math.floor(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const days = data[0]?.series.length ?? 0;

  const { bands, totals, grand } = useMemo(() => {
    const W = Math.max(box.w, 1), H = Math.max(box.h, FOOT + PAD_OUT + 1);
    const dayTotal = Array.from({ length: days }, (_, d) => data.reduce((n, u) => n + u.series[d], 0));
    const maxTotal = Math.max(1, ...dayTotal);
    const x = (d: number) => (d / Math.max(1, days - 1)) * W;
    // Flipped: 0 sits at the plot's base (FOOT below the seam) and load grows DOWN the page.
    const y = (v: number) => FOOT + (H - FOOT - PAD_OUT) * (v / maxTotal);

    // Stack from the seam outward in array order (biggest User first, so it anchors the seam).
    const lower = Array(days).fill(0);
    const bands = data.map((u, i) => {
      const upper = lower.map((lo, d) => lo + u.series[d]);
      const outer = upper.map((v, d) => ({ x: x(d), y: y(v) }));
      // The base band (i=0) runs up to the seam; outer bands close on the band inside them.
      const inner = lower.map((v, d) => ({ x: x(d), y: i === 0 ? 0 : y(v) })).reverse();
      const path = `M${outer[0].x},${outer[0].y}${smooth(outer)}L${inner[0].x},${inner[0].y}${smooth(inner)}Z`;
      for (let d = 0; d < days; d++) lower[d] = upper[d];
      return { name: u.username, color: rgb(pigment(u.balance, 0.5)), path };
    });

    const totals = data.map((u) => u.series.reduce((n, v) => n + v, 0));
    const grand = totals.reduce((n, v) => n + v, 0) || 1;
    return { bands, totals, grand };
  }, [data, days, box.w, box.h]);

  // The series ends today: index days-1 is today, index 0 is days-1 ago.
  const ticks = SEAM_TICKS.map((f) => {
    const d = Math.round(f * (days - 1));
    return { x: f * box.w, label: dayLabel(days - 1 - d) };
  });

  const mask = `${id}-mask`, shade = `${id}-shade`, wash = `${id}-wash`;

  return (
    <div className="md:contents">
      <div ref={boxRef} className="h-[13.75rem] w-full md:h-[16.25rem]">
        {box.w > 0 && (
          <svg width={box.w} height={box.h} viewBox={`0 0 ${box.w} ${box.h}`} className="block" style={edgeFade(0.2, 0, LEGEND_W)}>
            <defs>
              {/* Fades in over the top of the overlap (the Tide fades out over the bottom — see SEAM_FADE),
                  solid past it, then fading toward the far end. */}
              <linearGradient id={shade} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={box.h}>
                <stop offset="0" stopColor="#000" />
                <stop offset={(SEAM * SEAM_FADE * 0.5) / box.h} stopColor="#9a9a9a" />
                <stop offset={(SEAM * SEAM_FADE) / box.h} stopColor="#fff" />
                <stop offset={FOOT / box.h} stopColor="#f0f0f0" />
                <stop offset="1" stopColor="#3a3a3a" />
              </linearGradient>
              <linearGradient id={wash} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor={SEAM_WASH} />
                <stop offset="1" stopColor="rgba(255,226,165,0)" />
              </linearGradient>
              {/* Luminance mask: white keeps, black cuts — the dates are holes to the abyss. */}
              <mask id={mask} maskUnits="userSpaceOnUse" x="0" y="0" width={box.w} height={box.h}>
                <rect width={box.w} height={box.h} fill={`url(#${shade})`} />
                {ticks.map((t) => (
                  <text
                    key={t.x}
                    x={t.x}
                    y={SEAM + 17}
                    textAnchor="middle"
                    fill="#000"
                    style={{ fontSize: SEAM_FONT.size, fontWeight: SEAM_FONT.weight }}
                  >
                    {t.label}
                  </text>
                ))}
              </mask>
            </defs>
            <g mask={`url(#${mask})`}>
              {bands.map((b, i) => (
                <path
                  key={b.name}
                  d={b.path}
                  fill={b.color}
                  fillOpacity={hover < 0 ? 0.92 : hover === i ? 0.98 : 0.22}
                  style={{ transition: "fill-opacity 160ms" }}
                  onPointerEnter={() => setHover(i)}
                  onPointerLeave={() => setHover(-1)}
                />
              ))}
              <rect width={box.w} height={FOOT} fill={`url(#${wash})`} pointerEvents="none" />
            </g>
          </svg>
        )}
      </div>

      {/* Legend doubles as the readout — each User, their pigment, their share; the hovered band bolds.
          md+: floats over the right side-fade, centred on the seam (z-20 lifts it over the Tide's canvas,
          which overlaps the seam). Narrow: drops below the bands. */}
      <div
        className="mt-5 mb-4 flex flex-col gap-3 md:absolute md:right-0 md:z-20 md:my-0 md:w-[12%] md:pl-2 md:-translate-y-1/2"
        style={{ top: SEAM / 2 }}
      >
        <Tip className="label" text="Karma spent per person over the last 30 days. Measured in Karma rather than hours because an hour on the coveted card is not an hour on the quiet one.">
          Usage · last 30 days
        </Tip>
        <ul className="flex flex-wrap gap-x-6 gap-y-2 md:flex-col md:flex-nowrap">
          {bands.map((b, i) => (
            <li
              key={b.name}
              className="flex items-center gap-2 transition-opacity"
              style={{ opacity: hover < 0 || hover === i ? 1 : 0.4 }}
              onPointerEnter={() => setHover(i)}
              onPointerLeave={() => setHover(-1)}
            >
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: b.color }} />
              <span className="truncate text-[13px]" style={{ color: hover === i ? "var(--ink)" : "var(--ink-soft)" }}>
                {b.name}
              </span>
              <span className="num text-[12px] text-ink-mute">{Math.round((totals[i] / grand) * 100)}%</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
