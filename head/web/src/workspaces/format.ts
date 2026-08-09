import { WORKSPACE_ALLOWANCE_BYTES } from "../api";

// The 32 GB pool, re-exported so the page and the meter read one number. Unlike the Recipe wall this is
// not a single mass its owner fills: they divide it into a Storage each, one per Workspace, at creation.
export const ALLOWANCE = WORKSPACE_ALLOWANCE_BYTES;

export const GB = 1024 ** 3;

/** Bytes → "3.4 GB" / "820 MB". Same grain as the Recipe shelf: a Workspace holds a whole project. */
export function size(bytes: number): string {
  if (bytes >= GB) return `${(bytes / GB).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / (1024 ** 2)))} MB`;
}

/** Declared per-VISIT wall clock, minutes → "45m" / "3h". The workspace itself never expires. */
export function runtime(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const h = minutes / 60;
  return Number.isInteger(h) ? `${h}h` : `${h.toFixed(1)}h`;
}

/** VRAM, MB → "80 GB". A visit's declared need, never to the megabyte. */
export function vram(mb: number): string {
  return `${Math.round(mb / 1024)} GB`;
}

/**
 * How long since anyone was in here. "never" is a real and common answer — a Workspace can be created
 * and not visited for weeks, and unlike a Recipe's staleness that costs nothing; it is orientation, not
 * a warning.
 */
export function visited(ms: number | null): string {
  if (!ms) return "never visited";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return "visited just now";
  const m = s / 60;
  if (m < 90) return `visited ${Math.round(m)}m ago`;
  const h = m / 60;
  if (h < 36) return `visited ${Math.round(h)}h ago`;
  const d = h / 24;
  if (d < 14) return `visited ${Math.round(d)}d ago`;
  return `visited ${Math.round(d / 7)}w ago`;
}

/** What an inline control does to a Workspace. The page owns the mutation; cards just name the verb. */
export type WorkspaceAction = "open" | "edit" | "download" | "delete";
