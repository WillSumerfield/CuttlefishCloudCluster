// Demo data for the Tide — loaded ONLY under ?demo, so the page can be reviewed whole without a live
// cluster with a month of history. Two shapes: the Demand series the Head would serve (per Arm, hourly),
// and per-User usage (which has no endpoint at all — see Usage.tsx).

import type { DemandSample } from "../api";
import { members, type Member } from "../karma/mock";

const DAYS = 30;

// A believable working day: dead overnight, a mid-morning rise, twin humps either side of lunch, an
// evening decline. Returned as a multiplier on the Arm's base Demand.
function dayShape(hour: number): number {
  const bump = (c: number, w: number) => Math.exp(-((hour - c) ** 2) / (2 * w * w));
  const night = 0.28;
  return night + 1.5 * bump(11, 2.2) + 1.3 * bump(16, 2.4) + 0.5 * bump(20, 2.0);
}

// Cheap deterministic jitter so the series isn't glassy but is stable across reloads.
function wobble(a: number, b: number): number {
  const s = Math.sin(a * 12.9898 + b * 78.233) * 43758.5453;
  return s - Math.floor(s); // 0..1
}

const ARMS = [
  { node: "arm-cephalo-04", base: 1.7 }, // the coveted big card — runs hottest
  { node: "arm-siphon-02", base: 1.1 },
  { node: "arm-mantle-07", base: 1.4 },
  { node: "arm-tide-12", base: 0.6 }, // a quiet little card few jobs fit or want
];

/** The Demand series the Head's /karma/demand would return: per Arm, hourly, over the last 30 days. */
export function demoDemand(): DemandSample[] {
  const out: DemandSample[] = [];
  const start = new Date();
  for (let d = 0; d < DAYS; d++) {
    for (let h = 0; h < 24; h++) {
      const dt = new Date(start);
      dt.setDate(dt.getDate() - d);
      dt.setHours(h, 0, 0, 0);
      const weekend = dt.getDay() === 0 || dt.getDay() === 6 ? 0.45 : 1;
      const at = dt.toISOString();
      for (const a of ARMS) {
        const noise = 0.7 + wobble(d * 24 + h, a.base * 10) * 0.6;
        const demand = Math.max(0, Math.min(8, a.base * dayShape(h) * weekend * noise));
        out.push({ node: a.node, at, demand: +demand.toFixed(2) });
      }
    }
  }
  return out;
}

export type UsageUser = { name: string; balance: number; series: number[] }; // series: DAYS daily totals

/** Per-User compute usage over the last 30 days — the top users, warm pigment, oldest day first. */
export function demoUsage(): UsageUser[] {
  // The heaviest users, biggest first. Usage loosely tracks standing (creditors earn by spending).
  const top: Member[] = [...members].sort((a, b) => b.balance - a.balance).slice(0, 5);
  return top.map((m, i) => {
    const scale = 1 + Math.max(0, m.balance) / 500; // creditors consume more
    const series = Array.from({ length: DAYS }, (_, d) => {
      const day = DAYS - 1 - d; // oldest → newest, left → right
      const trend = 1 + 0.4 * Math.sin((day / DAYS) * Math.PI * 2 + i); // a slow personal rhythm
      const noise = 0.6 + wobble(day + i * 31, m.balance) * 0.8;
      return +(scale * 3 * trend * noise).toFixed(1);
    });
    return { name: m.name, balance: m.balance, series };
  });
}
