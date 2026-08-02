import { RECIPE_ALLOWANCE_BYTES, type Recipe } from "../api";

// The 24 GB wall, re-exported so the page and meter read one number. See api.ts for why it's a hard wall.
export const ALLOWANCE = RECIPE_ALLOWANCE_BYTES;

const GB = 1024 ** 3;
const MB = 1024 ** 2;

/** Bytes → "3.4 GB" / "820 MB". A Recipe holds a whole project, so GB with one decimal is the right grain. */
export function size(bytes: number): string {
  if (bytes >= GB) return `${(bytes / GB).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / MB))} MB`;
}

/** Declared wall-clock, minutes → "45m" / "3h" / "1.5h". The same ask a Spawn carries, read back short. */
export function runtime(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const h = minutes / 60;
  return Number.isInteger(h) ? `${h}h` : `${h.toFixed(1)}h`;
}

/** VRAM, MB → "80 GB". Mirrors the Arms formatter; a Recipe's declared need, never to the megabyte. */
export function vram(mb: number): string {
  return `${Math.round(mb / 1024)} GB`;
}

/**
 * How stale the frozen snapshot is — relative age of `frozen_at`. "just now" / "3h ago" / "5d ago". This
 * is the one fact that tells an owner the Recipe is running yesterday's code, so it earns its place.
 */
export function ago(ms: number): string {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return "just now";
  const m = s / 60;
  if (m < 90) return `${Math.round(m)}m ago`;
  const h = m / 60;
  if (h < 36) return `${Math.round(h)}h ago`;
  const d = h / 24;
  if (d < 14) return `${Math.round(d)}d ago`;
  return `${Math.round(d / 7)}w ago`;
}

/** What an inline control does to a Recipe. The page owns the mutation; cards just name the verb. */
export type RecipeAction = "run" | "update" | "edit" | "delete";
