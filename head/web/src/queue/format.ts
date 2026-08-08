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

/** "recoverable 21h" / "recoverable <1h" for a History row whose Recovery Window (docs/TODO.md) is
 *  still open, else null.
 *  NOT live-ticking — History is a static archive, fetched once and never polled (see HistoryPage), so
 *  this is computed at render time and simply goes stale like every other cell until the next visit.
 *
 *  `outputsLabel`'s shape minus its days branch, which a 24-hour window can never reach — so the two
 *  cells read as one family, while recovery keeps ending in hours and never rounds up into the "1d"
 *  that would make it look like a retention policy you could plan around (CONTEXT.md, Recovery Window). */
export function recoveryLabel(expiresAt: string | null | undefined): string | null {
  if (!expiresAt) return null;
  const hours = (new Date(expiresAt).getTime() - Date.now()) / 3_600_000;
  if (hours <= 0) return null;
  return hours < 1 ? "recoverable <1h" : `recoverable ${Math.round(hours)}h`;
}

/** "outputs 6d" / "outputs 20h" / "outputs <1h" for a Spawn whose declared results the Head still holds
 *  (docs/TODO.md "Storage retention"), else null. Same static-at-render-time rule as `recoveryLabel`.
 *
 *  Coarser than that one at the top end, deliberately: 7 days is a deadline a User plans around, not one
 *  they watch tick down. The final hours ARE worth their own shape, though — that is when it stops being
 *  an abstraction, which is the whole of what recovery ever shows. */
export function outputsLabel(expiresAt: string | null | undefined): string | null {
  if (!expiresAt) return null;
  const hours = (new Date(expiresAt).getTime() - Date.now()) / 3_600_000;
  if (hours <= 0) return null;
  if (hours < 1) return "outputs <1h";
  if (hours < 48) return `outputs ${Math.round(hours)}h`;
  return `outputs ${Math.round(hours / 24)}d`;
}
