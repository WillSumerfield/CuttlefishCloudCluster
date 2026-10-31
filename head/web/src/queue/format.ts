/** "40m", "6h", "6h04m", "—" — a declared `--time` in minutes, read by a person. */
export function duration(minutes: number | null): string {
  if (!minutes || minutes <= 0) return "—";
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h${String(m).padStart(2, "0")}m`;
}

/** The Arm's bare name, dropped of any "arm-" prefix noise is NOT done — the name is the name. But a
 *  null node (a Dive not yet placed) reads as "unplaced", never as blank. */
export function armLabel(node: string | null): string {
  return node ?? "unplaced";
}

/** The same, for a Dive that has already ENDED — where "unplaced" would be a lie. A finished Dive
 *  was placed; we simply may not have caught it. The meter records the placement, and a Dive that
 *  started and ended between two 30s passes was never metered, so nothing wrote it down. Say that,
 *  rather than claiming it never ran anywhere. */
export function placedLabel(node: string | null): string {
  return node ?? "arm unrecorded";
}
