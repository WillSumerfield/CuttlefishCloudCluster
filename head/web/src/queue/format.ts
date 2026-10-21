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
 *  null node (a Spawn not yet placed) reads as "unplaced", never as blank. */
export function armLabel(node: string | null): string {
  return node ?? "unplaced";
}

/** The same, for a Spawn that has already ENDED — where "unplaced" would be a lie. A finished Spawn
 *  was placed; we simply may not have caught it. The meter records the placement, and a Spawn that
 *  started and ended between two 30s passes was never metered, so nothing wrote it down. Say that,
 *  rather than claiming it never ran anywhere. */
export function placedLabel(node: string | null): string {
  return node ?? "arm unrecorded";
}

/** "outputs 6d" / "outputs 20h" / "outputs <1h" for a Spawn whose declared results the Head still holds
 *  (docs/TODO.md "Storage retention"), else null.
 *  NOT live-ticking — History is a static archive, fetched once and never polled (see HistoryPage), so
 *  this is computed at render time and goes stale like every other cell until the next visit. */
export function outputsLabel(expiresAt: string | null | undefined): string | null {
  if (!expiresAt) return null;
  const hours = (new Date(expiresAt).getTime() - Date.now()) / 3_600_000;
  if (hours <= 0) return null;
  if (hours < 1) return "outputs <1h";
  if (hours < 48) return `outputs ${Math.round(hours)}h`;
  return `outputs ${Math.round(hours / 24)}d`;
}
