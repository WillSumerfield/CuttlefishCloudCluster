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

/** "recoverable 12m" for a History row whose Recovery Window (docs/TODO.md) is still open, else null.
 *  NOT live-ticking — History is a static archive, fetched once and never polled (see HistoryPage), so
 *  this is computed at render time and simply goes stale like every other cell until the next visit. */
export function recoveryLabel(expiresAt: string | null | undefined): string | null {
  if (!expiresAt) return null;
  const left = Math.round((new Date(expiresAt).getTime() - Date.now()) / 60_000);
  return left > 0 ? `recoverable ${left}m` : null;
}

/** "outputs 6d" / "outputs 20h" / "outputs <1h" for a Spawn whose declared results the Head still holds
 *  (docs/TODO.md "Storage retention"), else null. Same static-at-render-time rule as `recoveryLabel`.
 *
 *  Coarser units than that one, deliberately: 7 days is a deadline a User plans around, not one they
 *  watch tick down, and "outputs 8640m" would say the same thing while reading as an emergency. The
 *  final hours ARE worth their own shape, though — that is when it stops being an abstraction. */
export function outputsLabel(expiresAt: string | null | undefined): string | null {
  if (!expiresAt) return null;
  const hours = (new Date(expiresAt).getTime() - Date.now()) / 3_600_000;
  if (hours <= 0) return null;
  if (hours < 1) return "outputs <1h";
  if (hours < 48) return `outputs ${Math.round(hours)}h`;
  return `outputs ${Math.round(hours / 24)}d`;
}
