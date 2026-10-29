import { useEffect, useRef } from "react";
import { useReducedMotion } from "../app/useReducedMotion";

/**
 * THE PASSING CLOUD — a working Dive. Dark bands drifting across the skin, meaning only THIS MACHINE IS
 * WORKING. Distinct from the bloom (cool, waiting).
 *
 * DISCRETE bands, not a texture: each is one `.cloud-band` element, appended by a JS timer, drifted the
 * full width once by WAAPI at constant speed, removed on exit. Emissions are STEADY (one every `gap` ms,
 * no bursts); each card's emitter gets a random first-delay phase offset so cards don't cross in lockstep.
 *
 * No canvas, no shader, no rAF: travel is a compositor transform, so an idle-but-clear card costs nothing
 * per frame. Reduced motion: emitter never starts — "working" is already said by the tick/column/Arm.
 */

// Defaults tuned for the queue's tall-ish shard. A caller on a differently-shaped card (the roster's
// wide, short row) overrides the shape dials via the `config` prop — see CloudConfig below.
const CLOUD_DEFAULTS = {
  /** Band width, as a fraction of the card. Fixed (variation is minimal — only the soft edges are seeded). */
  width: 0.6,
  /** Tilt of a band, degrees. Baked into the travel transform. */
  tilt: -10,
  /** Crossing time, ms. Brisk and uniform — every band the same. */
  cross: 1000,
  /** Gap between clouds, ms. A FIXED cadence: one cloud, then `gap` of quiet, then the next — no bursts,
   *  no randomness. The card shows a steady stream of evenly spaced clouds. */
  gap: 200,
  /** Band darkness / opacity: how far a band pulls the plate toward abyss ink. 0 = invisible, 1 = full abyss. */
  darkness: 0.2,
  /** Soft-edge alpha fade: fraction of EACH side (left AND right) that ramps transparent→ink. Clamped to
   *  [0, 0.5]: 0 = hard edges, 0.5 = a soft lens with no solid core (the most a symmetric two-sided fade can
   *  do — the edges meet in the middle). Applied to the leading AND trailing edges as a band travels. */
  edgeFade: 100.0,
};

/** Per-instance overrides for the shape/tempo of the cloud, so one component serves cards of any aspect. */
export type CloudConfig = Partial<typeof CLOUD_DEFAULTS>;

/**
 * Named presets for cards whose SHAPE breaks the defaults (tuned for the queue's tall-ish shard). Arms
 * roster is wide, short rows: default half-width band tilted -10° becomes a steep slash clipped to a
 * ~58px strip, and the tall-shard tempo stacks a whole procession on-screen. `roster` draws a narrow,
 * near-upright streak, slower and more spaced-out, so a wide row reads as one cloud passing, not a barcode.
 */
export const CLOUD_PRESETS = {
  roster: {
    width: 0.4,
    tilt: -2,
    // A wide row is a long way to cross; a brisk tempo would stack many narrow streaks side by side. Slow
    // the crossing and open the gap so it's one drifting streak at a time, with clear sky between.
    cross: 1500,
    gap: 300,
    darkness: 0.1,
    edgeFade: 0.8,
  },
} satisfies Record<string, CloudConfig>;

export function CloudDrift({ config }: { config?: CloudConfig } = {}) {
  const layerRef = useRef<HTMLDivElement | null>(null);
  const reduced = useReducedMotion();

  // Merge over the defaults, and hold the result in a ref the emitter reads at call time — so a caller
  // can retune without restarting (the effect depends only on `reduced`, never the config object's identity).
  const merged = { ...CLOUD_DEFAULTS, ...config };
  const cfgRef = useRef(merged);
  cfgRef.current = merged;

  useEffect(() => {
    // Reduced motion: no bands at all. Meaning is carried by the tick / column / Arm, never by this.
    if (reduced) return;
    const layer = layerRef.current;
    if (!layer) return;

    let alive = true;
    let timer = 0;

    const emit = () => {
      if (!alive) return;
      const CLOUD = cfgRef.current;
      const w = layer.clientWidth;
      if (w > 0) {
        const bw = w * CLOUD.width;
        const band = document.createElement("div");
        band.className = "cloud-band";
        band.style.width = `${bw}px`;
        // Edge alpha fade from `edgeFade` (a fraction per side), clamped to [0, 0.5] and kept inside each
        // half — left in [0,50]%, right in [50,100]% — so the two soft edges can NEVER cross. If they crossed
        // (e.g. edgeFade > 0.5, or jitter tipping them past each other), CSS clamps the out-of-order stops and
        // the gradient collapses to a one-sided ramp: fade behind, hard in front. This keeps both sides soft.
        const f = Math.min(Math.max(CLOUD.edgeFade, 0), 0.5) * 100;
        const jit = () => (Math.random() - 0.5) * 8; // small per-band nudge so bands aren't pixel-identical
        const edgeL = Math.min(50, Math.max(0, f + jit()));
        const edgeR = Math.max(50, Math.min(100, 100 - f + jit()));
        band.style.setProperty("--edge-l", `${edgeL}%`);
        band.style.setProperty("--edge-r", `${edgeR}%`);
        layer.appendChild(band);

        // Fully off the left edge → fully off the right edge, so every band makes the whole crossing. Tilt
        // rides along in the same transform (WAAPI owns `transform`; a class transform would be overwritten).
        const from = -bw * 1.5;
        const to = w + bw * 0.5;
        const rot = `rotate(${CLOUD.tilt}deg)`;
        const anim = band.animate(
          [{ transform: `translateX(${from}px) ${rot}` }, { transform: `translateX(${to}px) ${rot}` }],
          { duration: CLOUD.cross, easing: "linear" },
        );
        anim.onfinish = () => band.remove();
      }
      // Steady metronome: the next cloud is always exactly `gap` away. No followup roll, no burst.
      timer = window.setTimeout(emit, CLOUD.gap);
    };

    // Random first delay is a PHASE offset only — spread across a full cloud cycle (crossing + gap) so
    // every card starts at a genuinely different point in the stream, while the pace stays identical.
    const period = cfgRef.current.cross + cfgRef.current.gap;
    timer = window.setTimeout(emit, Math.random() * period);

    return () => {
      alive = false;
      clearTimeout(timer);
      layer.replaceChildren();
    };
  }, [reduced]);

  // `darkness` drives the ink opacity via a percentage the layer's --cloud-ink is mixed from (see index.css).
  return (
    <div
      ref={layerRef}
      aria-hidden
      className="cloud-layer"
      style={{ "--cloud-darkness": `${merged.darkness * 100}%` } as React.CSSProperties}
    />
  );
}
