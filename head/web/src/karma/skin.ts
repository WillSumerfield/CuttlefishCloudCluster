// The cephalopod skin — the flesh and its pigment, extracted so more than one surface can wear it.
//
// Standings paints a whole FIELD of members in this skin; the Account hero paints ONE bloom of it. Both
// need the same fleshy substrate (a warm fish-white gradient, pale leucophore mottling, an organic
// dissolving edge) and the same pigment ramp (umber → bronze → gold, keyed to a Karma balance), so those
// pure pieces live here and both callers import them. Everything here is deterministic and canvas-only —
// no React, no state — which is why it was safe to lift out of the Standings component whole.

export function seededRand(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0xffffffff;
  };
}

/* ---- organic edge & mottling noise ------------------------------------------------------------- */

export function valueNoise(seed: number) {
  const p = new Float32Array(256);
  const rand = seededRand(seed);
  for (let i = 0; i < 256; i++) p[i] = rand();
  const at = (x: number, y: number) => p[((x & 15) + ((y & 15) << 4)) & 255];
  const smooth = (t: number) => t * t * (3 - 2 * t);
  return (x: number, y: number) => {
    const xi = Math.floor(x), yi = Math.floor(y);
    const xf = smooth(x - xi), yf = smooth(y - yi);
    const a = at(xi, yi), b = at(xi + 1, yi), c = at(xi, yi + 1), d = at(xi + 1, yi + 1);
    return (a + (b - a) * xf) + ((c + (d - c) * xf) - (a + (b - a) * xf)) * yf;
  };
}

/** The knobs on the skin's dissolving edge. */
const EDGE = {
  warp: 0.3,
  lobes: 40,
  ripples: 100,
  inset: 0.85,
  fadeFrom: 0.6,
  squareness: 3.5,
  seed: 92,
};

// Alpha mask, built once per size at 1/3 resolution and scaled up. Black pixels with alpha = how much to
// ERASE; used with `destination-out` to dissolve the flesh + dots together into the abyss.
export function buildEdgeMask(w: number, h: number): HTMLCanvasElement {
  const scale = 3;
  const mw = Math.max(1, Math.ceil(w / scale)), mh = Math.max(1, Math.ceil(h / scale));
  const c = document.createElement("canvas");
  c.width = mw; c.height = mh;
  const ctx = c.getContext("2d")!;
  const img = ctx.createImageData(mw, mh);
  const noise = valueNoise(EDGE.seed);

  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      const nx = (x / mw) * 2 - 1, ny = (y / mh) * 2 - 1;
      const n = EDGE.squareness;
      const ax = Math.abs(nx) / EDGE.inset, ay = Math.abs(ny) / EDGE.inset;
      let d = Math.pow(Math.pow(ax, n) + Math.pow(ay, n), 1 / n);
      const f = noise(x / EDGE.lobes, y / EDGE.lobes) * 0.7 + noise(x / EDGE.ripples, y / EDGE.ripples) * 0.3;
      d += (f - 0.5) * EDGE.warp;
      const t = Math.max(0, Math.min(1, (d - EDGE.fadeFrom) / (1 - EDGE.fadeFrom)));
      let keep = 1 - t * t * (3 - 2 * t);
      const rim = Math.max(Math.abs(nx), Math.abs(ny));
      const g = Math.max(0, Math.min(1, (rim - 0.9) / 0.1));
      keep *= 1 - g * g * (3 - 2 * g);
      const i = (y * mw + x) * 4;
      img.data[i + 3] = ((1 - keep) * 255) | 0;
    }
  }
  ctx.putImageData(img, 0, 0);
  return c;
}

const FLESH_OPACITY = 0.8;

// The pale leucophore mottling laid over the flesh. Each surface may want its own reading of it, so the
// knobs are a value passed into paintFlesh rather than a fixed constant. WHITE is the Standings default.
export type Whiteness = {
  rgb: string;
  scale: number;
  detail: number;
  amount: number;
  lo: number;
  hi: number;
  seed: number;
};

export const WHITE: Whiteness = {
  rgb: "255, 246, 236",
  scale: 100,
  detail: 30,
  amount: 0.5,
  lo: 0.2,
  hi: 0.9,
  seed: 771,
};

function buildWhitenessMap(w: number, h: number, white: Whiteness): HTMLCanvasElement {
  const scale = 3;
  const mw = Math.max(1, Math.ceil(w / scale)), mh = Math.max(1, Math.ceil(h / scale));
  const c = document.createElement("canvas");
  c.width = mw; c.height = mh;
  const ctx = c.getContext("2d")!;
  const img = ctx.createImageData(mw, mh);
  const noise = valueNoise(white.seed);
  const [r, g, b] = white.rgb.split(",").map((s) => parseInt(s, 10));
  const smooth = (t: number) => t * t * (3 - 2 * t);

  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      const px = x * scale, py = y * scale;
      const f = noise(px / white.scale, py / white.scale) * 0.7 + noise(px / white.detail, py / white.detail) * 0.3;
      const t = Math.max(0, Math.min(1, (f - white.lo) / (white.hi - white.lo)));
      const a = smooth(t) * white.amount;
      const i = (y * mw + x) * 4;
      img.data[i] = r; img.data[i + 1] = g; img.data[i + 2] = b;
      img.data[i + 3] = (a * 255) | 0;
    }
  }
  ctx.putImageData(img, 0, 0);
  return c;
}

// The fleshy fish-white skin the chromatophores sit in — drawn into the canvas, never a flat colour.
// `white` tunes the leucophore mottling; callers that want their own reading pass their own (Account does).
export function paintFlesh(ctx: CanvasRenderingContext2D, w: number, h: number, white: Whiteness = WHITE) {
  ctx.save();
  ctx.globalAlpha = FLESH_OPACITY;
  const base = ctx.createLinearGradient(0, 0, w * 0.35, h);
  base.addColorStop(0, "rgb(245, 220, 206)");
  base.addColorStop(0.55, "rgb(232, 200, 188)");
  base.addColorStop(1, "rgb(220, 190, 178)");
  ctx.fillStyle = base;
  ctx.fillRect(0, 0, w, h);

  const glows: [number, number, number, number, string, number][] = [
    [0.28, 0.32, 0.60, 0.45, "255, 232, 214", 0.95],
    [0.78, 0.30, 0.55, 0.50, "255, 220, 210", 0.85],
    [0.60, 0.78, 0.65, 0.55, "248, 214, 198", 0.90],
    [0.18, 0.82, 0.45, 0.40, "240, 208, 196", 0.75],
    [0.88, 0.72, 0.50, 0.45, "252, 226, 208", 0.85],
  ];
  for (const [cx, cy, rx, ry, rgb, a] of glows) {
    ctx.save();
    ctx.translate(cx * w, cy * h);
    ctx.scale((rx * w) / 100, (ry * h) / 100);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, 100);
    g.addColorStop(0, `rgba(${rgb}, ${a})`);
    g.addColorStop(0.7, `rgba(${rgb}, 0)`);
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(0, 0, 100, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  ctx.drawImage(buildWhitenessMap(w, h, white), 0, 0, w, h);
  ctx.restore();
}

/** Balances run roughly ±1000. The ramp works in −1..1, so normalise before mapping to colour. */
export const KARMA_RANGE = 10000;

// pigment colour for a member: umber (debt) → bronze (neutral) → gold (creditor)
export function pigment(balance: number, jitter: number): [number, number, number] {
  const b = Math.max(-1, Math.min(1, balance / KARMA_RANGE));
  const t = (b + 1) / 2 + (jitter - 0.5) * 0.18;
  const clamped = Math.max(0, Math.min(1, t));
  const stops = [
    [0.29, 0.18, 0.09],
    [0.63, 0.44, 0.17],
    [0.94, 0.76, 0.35],
  ];
  const seg = clamped < 0.5 ? 0 : 1;
  const k = clamped < 0.5 ? clamped * 2 : (clamped - 0.5) * 2;
  const a = stops[seg]; const c = stops[seg + 1];
  return [
    a[0] + (c[0] - a[0]) * k,
    a[1] + (c[1] - a[1]) * k,
    a[2] + (c[2] - a[2]) * k,
  ];
}
