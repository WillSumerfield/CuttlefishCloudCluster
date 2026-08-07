import type { Arm } from "../api";

/** VRAM, MB → "80 GB" (whole GB — nobody reads a card's memory to the megabyte). "—" when unknown. */
export function vram(mb: number | null): string {
  if (!mb) return "—";
  return `${Math.round(mb / 1024)} GB`;
}

/** Power, the benchmarked TFLOP/s. "312 TF" / "—". Null until an Arm has been benchmarked. */
export function power(tf: number | null): string {
  if (!tf) return "—";
  return `${Math.round(tf)} TF`;
}

/** Mesh address, or "—" for an Arm not yet on the Mesh (awaiting its donor). */
export function mesh(ip: string | null): string {
  return ip || "—";
}

// The three motion tones an Arm can read as, plus the quiet ones. `live` is the only one that MOVES on
// the roster (the passing cloud) — a machine that is working. `wait` and `idle` are still; `alarm` is a
// faulted machine, the one state worth being loud about. Colour is spent only on live (irid) and alarm.
export type Tone = "live" | "idle" | "wait" | "alarm";

/**
 * Read an Arm's state into a word a person recognises, a tone, and whether it's working (→ the cloud).
 * squeue joins state with '+', and our own lifecycle words (awaiting-donor, benchmarking, retracted)
 * ride the same field, so match loosely.
 */
export function armState(a: Arm): { word: string; tone: Tone; working: boolean } {
  const s = (a.state || "").toLowerCase();
  // Checked FIRST, ahead of raw sinfo: retraction is app-layer (no real Slurm DRAIN — see
  // retraction.py), so `state` never says so on its own, and a Spawn still finishing out its grace
  // period may still read "allocated" underneath. The owner paused it; that's what the card shows.
  if (a.retracted) return { word: "retracted", tone: "wait", working: false };
  if (/alloc|mixed|running|completing/.test(s)) return { word: "allocated", tone: "live", working: true };
  if (/bench/.test(s)) return { word: "benchmarking", tone: "live", working: true };
  if (/down|drain|fail|error|invalid/.test(s)) return { word: "down", tone: "alarm", working: false };
  if (/awaiting-donor/.test(s) || (!a.admitted && !a.mesh_ip)) return { word: "awaiting donor", tone: "wait", working: false };
  if (/awaiting-admit/.test(s) || !a.admitted) return { word: "awaiting admit", tone: "wait", working: false };
  return { word: "idle", tone: "idle", working: false };
}

/** Dot colour per tone. live = machine iridescence; alarm = the one red; the quiet states get muted ink. */
export const toneColor: Record<Tone, string> = {
  live: "var(--irid-dim)",
  idle: "var(--ink-mute)",
  wait: "var(--ink-mute)",
  alarm: "var(--alarm)",
};

// One grid template, shared by the label head and every row, so the columns line up without a single
// rule between them (there are no rules anywhere). NAME · OWNER · STATE · GPUS · VRAM · POWER · MESH.
// Numeric columns are fixed-width and right-aligned; the text columns flex.
export const ROSTER_COLS =
  "minmax(150px,1.6fr) minmax(110px,1fr) 80px 80px 80px 80px 80px";

/** What the Dashboard can do to an Arm — the complete set, and it is deliberately small. Admitting,
 *  benchmarking and removing an Arm are Head-CLI-only and permanently so: the Head API exposes no
 *  admin-write endpoints, because a stolen Session must not be able to reshape the fleet. */
export type ArmAction = "retract" | "return";
