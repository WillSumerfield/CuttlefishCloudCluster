// Folding the Tide, and colouring it.
//
// The Head serves Demand over CLOCK time (api.getDemand → {node, at, demand} per Arm, hourly buckets).
// It deliberately serves no forecast — "a User reads the shape and infers the quiet hours for themselves".
// So the fold IS the forecast, done here: collapse the window into a typical DAY (24 hour-of-day buckets),
// per Arm and meaned across Arms, and read the current hour against the day's own average.
//
// The multiplier is the whole point: cost ∝ Demand (karma.cost = seconds·gpus·power·demand/3600), so
// "this hour's Demand ÷ the day's mean Demand" is exactly how much a Dive costs now versus a typical
// hour. It is a ratio, never a price — the model never quotes, it only ranks.

import type { DemandSample } from "../api";

export type ArmFold = { node: string; hours: number[] }; // 24 hour-of-day means

export type Fold = {
  mean: number[]; // 24 — Demand meaned across Arms, by hour of day. The waterline.
  perArm: ArmFold[]; // each Arm's own typical day, for the ghost lines
  dayMean: number; // the 24h average of `mean` — the "typical hour" the multiplier divides by
  peak: number; // max over mean + all Arms, for scaling the panel
  nowHour: number; // the viewer's local hour, where the now-marker sits
  multiplier: number; // mean[nowHour] / dayMean, bounded by TIDE_MAX — cost now vs a typical hour
};

const HOURS = 24;
const COLD = 1.0; // the honest cold-start Demand: no history forecasts nothing but "ordinary" (karma.tide)

/**
 * Ceiling on a displayed ratio. Matches `karma.DEMAND_MAX` by value, NOT by kind — that caps one Arm's
 * Demand, this caps a ratio of two Demands, and nothing makes them the same bound. Shared anyway because
 * the reason is shared: past ~8× the signal is saturated and the number has stopped carrying information.
 *
 * It bites on a cluster that is idle most of the time. Demand on an unwanted Arm is exactly 0 and the tick
 * records that zero every 30s, so a fleet that runs in bursts has a `dayMean` near zero — and a ratio
 * against near-zero runs away. The cap bounds what gets PRINTED; it does not make the denominator mean
 * more than it does. Colour is unaffected either way: `tideOklch` saturates at 1.7×.
 */
export const TIDE_MAX = 8;

/** A Demand ratio for display: `value` against the typical hour, bounded. `|| 1` catches 0/0 on a cold
 *  cluster, where "ordinary" is the only honest reading. */
export const ratioOf = (value: number, dayMean: number): number =>
  Math.min(TIDE_MAX, value / dayMean || 1);

/** Fold a clock-time series into the typical-day Tide. `nowHour` is injectable for tests. */
export function fold(samples: DemandSample[], nowHour = new Date().getHours()): Fold {
  // node → per-hour running sum + count. Sample hour is the viewer's LOCAL hour (getHours), so the Tide
  // reads in the clock the person is actually looking at.
  const acc = new Map<string, { sum: number[]; cnt: number[] }>();
  for (const s of samples) {
    const h = new Date(s.at).getHours();
    let e = acc.get(s.node);
    if (!e) { e = { sum: Array(HOURS).fill(0), cnt: Array(HOURS).fill(0) }; acc.set(s.node, e); }
    e.sum[h] += s.demand;
    e.cnt[h] += 1;
  }

  const perArm: ArmFold[] = [...acc].map(([node, e]) => ({
    node,
    hours: e.sum.map((s, h) => (e.cnt[h] ? s / e.cnt[h] : NaN)), // NaN = unseen hour for this Arm
  }));

  // Mean across the Arms that actually have a reading each hour. An hour no Arm saw is a gap (NaN).
  let mean = Array.from({ length: HOURS }, (_, h) => {
    const vals = perArm.map((a) => a.hours[h]).filter((v) => !Number.isNaN(v));
    return vals.length ? vals.reduce((n, v) => n + v, 0) / vals.length : NaN;
  });

  const known = mean.filter((v) => !Number.isNaN(v));
  if (known.length === 0) {
    // Cold cluster / live with no history: a flat, honest Tide. Multiplier is exactly 1.0.
    mean = Array(HOURS).fill(COLD);
  } else {
    mean = fillGaps(mean, known.reduce((n, v) => n + v, 0) / known.length);
  }

  // Fill each Arm's gaps too, so the ghost lines are continuous (fall back to the mean line at that hour).
  for (const a of perArm) a.hours = a.hours.map((v, h) => (Number.isNaN(v) ? mean[h] : v));

  const dayMean = mean.reduce((n, v) => n + v, 0) / HOURS || COLD;
  const peak = Math.max(...mean, ...perArm.flatMap((a) => a.hours), dayMean * 1.15);
  const multiplier = ratioOf(mean[nowHour], dayMean);

  return { mean, perArm, dayMean, peak, nowHour, multiplier };
}

/** Linear-interpolate NaN gaps, cyclically (the day wraps at midnight); lone-island fallback = `fill`. */
function fillGaps(a: number[], fill: number): number[] {
  const out = a.slice();
  for (let i = 0; i < a.length; i++) {
    if (!Number.isNaN(out[i])) continue;
    let prev = -1, next = -1;
    for (let k = 1; k < a.length; k++) {
      if (prev < 0 && !Number.isNaN(a[(i - k + a.length) % a.length])) prev = (i - k + a.length) % a.length;
      if (next < 0 && !Number.isNaN(a[(i + k) % a.length])) next = (i + k) % a.length;
    }
    out[i] = prev < 0 || next < 0 ? fill : (a[prev] + a[next]) / 2;
  }
  return out;
}

export type Level = "low" | "mid" | "high";

/** Low Tide (cheap) below 0.8×, High Tide (dear) above 1.3×, Mid Tide between. */
export function levelOf(multiplier: number): Level {
  if (multiplier < 0.8) return "low";
  if (multiplier > 1.3) return "high";
  return "mid";
}

export const LEVEL_LABEL: Record<Level, string> = { low: "Low Tide", mid: "Mid Tide", high: "High Tide" };

// The iridescence ramp, keyed to a Demand RATIO (value ÷ day mean): teal (quiet) → cyan (ordinary, 1×)
// → violet (fought over). These are the exact --irid-low/mid/high tokens from index.css; oklch() is a
// valid canvas fillStyle in every current engine, so the page and the canvas colour from one source.
const LOW: [number, number, number] = [0.68, 0.1, 180]; // teal
const MID: [number, number, number] = [0.78, 0.12, 210]; // cyan
const HIGH: [number, number, number] = [0.62, 0.17, 285]; // violet

const clamp01 = (t: number) => Math.max(0, Math.min(1, t));
const lerp3 = (a: [number, number, number], b: [number, number, number], t: number): [number, number, number] =>
  [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];

/** A CSS/canvas oklch() string for a Demand ratio. 0.5×→teal, 1×→cyan, ≥1.7×→violet, smooth between. */
export function tideOklch(ratio: number, alpha = 1): string {
  const [l, c, h] =
    ratio <= 1 ? lerp3(LOW, MID, clamp01((ratio - 0.5) / 0.5)) : lerp3(MID, HIGH, clamp01((ratio - 1) / 0.7));
  return `oklch(${l.toFixed(3)} ${c.toFixed(3)} ${h.toFixed(1)}${alpha < 1 ? ` / ${alpha}` : ""})`;
}
