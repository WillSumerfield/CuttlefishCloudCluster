import { useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "../app/useReducedMotion";
import { members, you, type Member } from "./mock";

/**
 * The signature surface.
 *
 * A dense field of small block-dot chromatophores. Each member occupies a
 * gaussian-radius patch of the field; nearby dots grow, and pick up that
 * person's pigment (warm yellow-brown, umber ↔ gold, no red).
 *
 * The cursor leaves a ripple wake — recent cursor samples radially displace
 * and lift dots, decaying over ~1s. No hairlines, no borders, everything
 * fades softly at the edges.
 *
 * There are no numbers on this surface. Colour does all the work.
 */

type Dot = { x: number; y: number; r: number; owner: number };

/**
 * The canvas is BLEED px larger than its layout box on every side, and is positioned absolutely so
 * that overhang doesn't push the ledger or the standing card around. The organic fade then happens
 * OUTSIDE the box, which means the solid, fully-dense part of the field fills the whole slot instead
 * of dissolving well before it reaches the edges.
 */
const BLEED = 170;

/** The knobs worth tuning on the skin itself. Edit, save, watch. */
const SKIN = {
  /** How far the cursor's swell reaches, in px. */
  reach: 40,

  /** How much bigger a dot directly under the cursor gets. */
  swell: 1.0,

  /**
   * How the swell falls off with distance. 2 is a plain gaussian — a tight bump with a short tail.
   * BELOW 2 the tail runs longer, so the swell fades out more gradually instead of stopping dead.
   */
  falloff: 1.0,

  /** Dot size when it belongs to nobody, and at a member's fully-expanded centre. */
  off: 0.6,
  on: 2.7,
};

function seededRand(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0xffffffff;
  };
}

/* ---- organic edge ------------------------------------------------------------------------------
 * The skin has no silhouette. It occupies a rectangular slot, but its edge is warped by noise, so it
 * reaches further into the abyss in some places than others and dissolves entirely at the rim.
 *
 * The flesh and the dots are both drawn into the canvas so that ONE mask can erase them together —
 * a CSS mask can't alpha-blend a div's background and its canvas child against the shader behind.
 */

function valueNoise(seed: number) {
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

/**
 * Where the members sit in the field.
 *
 * A jittered grid, then relaxation. The grid guarantees the field is COVERED (rejection sampling
 * doesn't — it only guarantees nobody overlaps, so it clumps); the jitter and the relaxation
 * together stop it from looking like a grid.
 */
const PLACEMENT = {
  /** Keep-out band at the edges of the box, in px. Nobody is placed inside these. */
  marginX: 250,
  marginY: 10,

  /**
   * The closest two members may end up, in px. This is enforced AFTER jittering, by pushing
   * offenders apart. It is the strongest dial here.
   *
   * If you set it larger than the box can accommodate for this many members, everyone gets shoved
   * outward until they jam against the margins — i.e. they pile up at the edges. That is the signal
   * that this number is too big, not that something is broken.
   */
  minSpacing: 200,

  /**
   * How far a member may wander from its cell centre, as a fraction of the cell. 0 = a rigid grid,
   * 1 = anywhere in its cell. Note this fights `minSpacing`: high jitter creates collisions that
   * relaxation then undoes, so if jitter seems to do nothing, `minSpacing` is what's eating it.
   */
  jitter: 0.6,

  /** How hard the relaxation pushes. More passes = closer to a perfectly even spread. */
  passes: 3,

  /** Change for an entirely different arrangement with the same character. */
  seed: 90213,
};

/** The knobs on the Shoal's dissolving edge. Edit, save, watch. */
const EDGE = {
  /**
   * How ragged the edge is — how far the noise displaces it, as a fraction of the half-size.
   * 0 = a clean superellipse, no wobble at all. 0.34 = current. Above ~0.5 it starts tearing holes
   * in the field, because the displacement outruns the width of the fade band.
   */
  warp: 0.3,

  /**
   * Size of the wobble. LOW = a few big slow lobes; HIGH = many small ripples along the edge.
   * These are the two noise octaves; the first dominates (70/30).
   */
  lobes: 40,
  ripples: 100,

  /**
   * How far in the organic edge sits, as a fraction of the half-size. LOWER = the skin occupies
   * less of the canvas and the fade starts earlier. Must leave room for `warp` to swing outward,
   * or the fade won't finish before the canvas runs out and you get a hard straight cut.
   */
  inset: 0.85,

  /**
   * Where the dissolve begins, along the same 0..1 distance. LOWER = a wider, softer, mistier fade
   * band; closer to 1 = a tighter, more defined edge. (1.0 is where the edge nominally is.)
   */
  fadeFrom: 0.6,

  /**
   * Squareness of the footprint. 2 = an ellipse. 5 = roughly rectangular with soft corners.
   * Large values approach a hard rectangle.
   */
  squareness: 3.5,

  /** Random seed for the noise. Change it for a different edge with the same character. */
  seed: 92,
};

// Alpha mask, built once per size at 1/3 resolution and scaled up (cheap, and the interpolation
// softens it further). Black pixels with alpha = how much to ERASE; used with `destination-out`.
function buildEdgeMask(w: number, h: number): HTMLCanvasElement {
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

      // Superellipse distance, scaled by `inset` so that even where the noise pushes the edge
      // outward, the fade still COMPLETES before the canvas runs out — otherwise the rect clips a
      // half-opaque edge and you get a hard straight cut.
      const n = EDGE.squareness;
      const ax = Math.abs(nx) / EDGE.inset, ay = Math.abs(ny) / EDGE.inset;
      let d = Math.pow(Math.pow(ax, n) + Math.pow(ay, n), 1 / n);

      // Warp the edge with two octaves of noise so it is never a clean curve.
      const f = noise(x / EDGE.lobes, y / EDGE.lobes) * 0.7 + noise(x / EDGE.ripples, y / EDGE.ripples) * 0.3;
      d += (f - 0.5) * EDGE.warp;

      // Keep the core solid, dissolve the rim.
      const t = Math.max(0, Math.min(1, (d - EDGE.fadeFrom) / (1 - EDGE.fadeFrom)));
      let keep = 1 - t * t * (3 - 2 * t);

      // Guard: nothing survives to the canvas boundary, whatever the noise did.
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

// The fleshy fish-white skin the chromatophores sit in — drawn into the canvas, never a flat colour.
function paintFlesh(ctx: CanvasRenderingContext2D, w: number, h: number) {
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
}

/** Balances run roughly ±1000. The ramp works in −1..1, so normalise before mapping to colour. */
const KARMA_RANGE = 1000;

// pigment colour for a member: umber (debt) → bronze (neutral) → gold (creditor)
function pigment(balance: number, jitter: number): [number, number, number] {
  // Normalise, THEN clamp. Clamping the raw ±1000 value to ±1 saturated every member to a ramp end,
  // so everyone in credit looked identically gold and everyone in debt identically umber.
  const b = Math.max(-1, Math.min(1, balance / KARMA_RANGE));
  // randomise the visible value slightly per-dot so no exact number can be read
  const t = (b + 1) / 2 + (jitter - 0.5) * 0.18;
  const clamped = Math.max(0, Math.min(1, t));
  // umber  #4a2d15   → bronze #a06f2b → gold #f0c25a
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

export function Standings() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const mouseRef = useRef<{ x: number; y: number; inside: boolean }>({ x: -9999, y: -9999, inside: false });
  // Last pointer activity, and a handle to revive the parked render loop when the pointer returns.
  const activeRef = useRef(0);
  const startRef = useRef<(() => void) | null>(null);
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const reduced = useReducedMotion();

  // Poisson-ish dot field, generated once per size. All coordinates are CANVAS space, which is the
  // layout box inflated by BLEED on every side.
  const field = useMemo(() => {
    const rand = seededRand(2027);
    const dots: Dot[] = [];
    const boxW = Math.max(size.w, 800);
    const boxH = Math.max(size.h, 360);
    const w = boxW + BLEED * 2;
    const h = boxH + BLEED * 2;

    // Uniform grid-jitter. Density stays flat across the whole field — the organic alpha mask does
    // all the dissolving, so thinning the dots as well would double-fade and hollow out the edges.
    const step = 13;
    for (let y = step / 2; y < h - step / 2; y += step) {
      for (let x = step / 2; x < w - step / 2; x += step) {
        if (rand() > 0.94) continue; // a few gaps, so the grid never reads as a grid
        const jx = (rand() - 0.5) * step * 0.85;
        const jy = (rand() - 0.5) * step * 0.85;
        const r = 1.15 + rand() * 0.55;
        dots.push({ x: x + jx, y: y + jy, r, owner: -1 });
      }
    }

    /*
     * Members are placed across the VISIBLE box only — never out in the bleed, where they'd be
     * half-dissolved.
     *
     * A JITTERED GRID, not rejection sampling. Rejection sampling only guarantees that no two
     * members are too close; it says nothing about covering the space, so it reliably produces
     * clumps in one corner and a dead void in another. A grid guarantees coverage by construction:
     * one member per cell, placed at a random point inside it. The cells are then relaxed apart so
     * the result never reads as a grid.
     */
    const { marginX, marginY, minSpacing, jitter: JITTER, passes, seed } = PLACEMENT;
    const rand2 = seededRand(seed);
    const fieldW = boxW - marginX * 2;
    const fieldH = boxH - marginY * 2;
    const n = members.length;

    // Cell grid proportioned to the box, so a wide monitor gets wide-and-short rather than square.
    const cols = Math.max(1, Math.round(Math.sqrt((n * fieldW) / Math.max(fieldH, 1))));
    const rows = Math.ceil(n / cols);
    const cellW = fieldW / cols;
    const cellH = fieldH / rows;

    // Shuffle the cells so member order doesn't correlate with position (rank must not be readable
    // off the layout — the Shoal shows standing by colour alone).
    const cells: number[] = [];
    for (let i = 0; i < cols * rows; i++) cells.push(i);
    for (let i = cells.length - 1; i > 0; i--) {
      const j = Math.floor(rand2() * (i + 1));
      [cells[i], cells[j]] = [cells[j], cells[i]];
    }

    const pts: { x: number; y: number }[] = [];
    for (let i = 0; i < n; i++) {
      const cell = cells[i];
      const cx = cell % cols, cy = Math.floor(cell / cols);
      const jx = (rand2() - 0.5) * cellW * JITTER;
      const jy = (rand2() - 0.5) * cellH * JITTER;
      pts.push({
        x: BLEED + marginX + (cx + 0.5) * cellW + jx,
        y: BLEED + marginY + (cy + 0.5) * cellH + jy,
      });
    }

    // Relax: push any pair closer than `minSpacing` back apart, then clamp everyone inside the
    // margins. This is what fights `jitter` — the two dials pull against each other by design.
    const loX = BLEED + marginX, hiX = BLEED + boxW - marginX;
    const loY = BLEED + marginY, hiY = BLEED + boxH - marginY;
    for (let pass = 0; pass < passes; pass++) {
      for (let i = 0; i < n; i++) {
        for (let j = i + 1; j < n; j++) {
          const dx = pts[j].x - pts[i].x, dy = pts[j].y - pts[i].y;
          const d = Math.hypot(dx, dy) || 0.001;
          if (d >= minSpacing) continue;
          const push = (minSpacing - d) / 2;
          const ux = dx / d, uy = dy / d;
          pts[i].x -= ux * push; pts[i].y -= uy * push;
          pts[j].x += ux * push; pts[j].y += uy * push;
        }
      }
      for (const p of pts) {
        p.x = Math.max(loX, Math.min(hiX, p.x));
        p.y = Math.max(loY, Math.min(hiY, p.y));
      }
    }

    const bloom = Math.min(boxW, boxH) * 0.06;
    const memberPositions = members.map((m, i) => ({
      m,
      x: pts[i].x,
      y: pts[i].y,
      radius: bloom * (1.0 + rand2() * 0.8),
    }));

    return { dots, memberPositions, w, h };
  }, [size.w, size.h]);

  // Precompute per-dot owner + base colour once field/members set
  const baked = useMemo(() => {
    const dots = field.dots;
    const jitter = new Float32Array(dots.length);
    const colors = new Float32Array(dots.length * 3);
    const ownerIdx = new Int16Array(dots.length);
    const gaussBoost = new Float32Array(dots.length);
    const rand = seededRand(4242);

    for (let i = 0; i < dots.length; i++) {
      const d = dots[i];
      jitter[i] = rand();
      // find nearest member; store gaussian weight for size boost
      let bestI = -1, bestW = 0;
      for (let mi = 0; mi < field.memberPositions.length; mi++) {
        const p = field.memberPositions[mi];
        const dx = d.x - p.x, dy = d.y - p.y;
        const dist2 = dx * dx + dy * dy;
        const sigma = p.radius;
        const w = Math.exp(-dist2 / (2 * sigma * sigma));
        if (w > bestW) { bestW = w; bestI = mi; }
      }

      // Unused dots (far from any member) are black speckle on the fleshy skin.
      // Member-adjacent dots pick up that member's pigment.
      let r: number, g: number, b: number;
      if (bestW < 0.08 || bestI < 0) {
        r = 0.02; g = 0.02; b = 0.03;
      } else {
        // Raw balance — `pigment` normalises by KARMA_RANGE itself. Dividing here too would scale it
        // by a million and collapse every member onto the middle of the ramp.
        const [pr, pg, pb] = pigment(field.memberPositions[bestI].m.balance, jitter[i]);
        // mix a bit toward black at the fringe of a member's gaussian
        const k = Math.min(1, bestW * 1.4);
        r = pr * k + 0.02 * (1 - k);
        g = pg * k + 0.02 * (1 - k);
        b = pb * k + 0.03 * (1 - k);
      }
      colors[i*3] = r; colors[i*3+1] = g; colors[i*3+2] = b;
      ownerIdx[i] = bestI;
      gaussBoost[i] = bestW; // 0..1
    }
    return { jitter, colors, ownerIdx, gaussBoost };
  }, [field]);


  // Resize observer
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const r = el.getBoundingClientRect();
      setSize({ w: Math.floor(r.width), h: Math.floor(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Draw loop
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !size.w || !size.h) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const W = field.w, H = field.h;
    // 1.5 not 2: the field is a soft, out-of-focus texture, so the extra rasterisation area a 2×
    // buffer costs (4× the fill work) buys nothing the eye can find here.
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.scale(dpr, dpr);

    const edgeMask = buildEdgeMask(W, H);

    const { reach: REACH, swell: SWELL, falloff: FALLOFF, off: OFF, on: ON } = SKIN;

    const dots = field.dots;
    const { jitter, colors, gaussBoost } = baked;

    // A dot is "live" if it sits in a member's gaussian (same 0.08 cut the colour bake uses to decide
    // black speck vs pigment). Only the live dots — a couple thousand, not ~ten — animate every frame;
    // the rest are the dead speckle, which never changes and is baked ONCE below.
    const live: number[] = [];
    for (let i = 0; i < dots.length; i++) if (gaussBoost[i] >= 0.08) live.push(i);

    // Draw one dot at its current radius/colour. Shared by the bake and the per-frame pass.
    const paintDot = (i: number, r: number, lift: number) => {
      const cr = Math.min(1, colors[i * 3] * lift);
      const cg = Math.min(1, colors[i * 3 + 1] * lift);
      const cb = Math.min(1, colors[i * 3 + 2] * lift);
      ctx.fillStyle = `rgb(${(cr * 255) | 0}, ${(cg * 255) | 0}, ${(cb * 255) | 0})`;
      ctx.beginPath();
      ctx.arc(dots[i].x, dots[i].y, r, 0, Math.PI * 2);
      ctx.fill();
    };

    // ---- the static base: flesh + every dead speck at rest, edge already dissolved. Painted once,
    // then blitted each frame in a single drawImage instead of ~8000 arc fills.
    const base = document.createElement("canvas");
    base.width = W; base.height = H;
    const bctx = base.getContext("2d")!;
    paintFlesh(bctx, W, H);
    for (let i = 0; i < dots.length; i++) {
      if (gaussBoost[i] >= 0.08) continue; // live dots are drawn per-frame, on top
      const d = dots[i];
      const r = d.r * OFF;
      const cr = Math.min(1, colors[i * 3]);
      const cg = Math.min(1, colors[i * 3 + 1]);
      const cb = Math.min(1, colors[i * 3 + 2]);
      bctx.fillStyle = `rgb(${(cr * 255) | 0}, ${(cg * 255) | 0}, ${(cb * 255) | 0})`;
      bctx.beginPath();
      bctx.arc(d.x, d.y, r, 0, Math.PI * 2);
      bctx.fill();
    }

    let raf = 0;
    let running = false;
    // While a scroll is in flight, hold the last painted frame instead of repainting: the canvas is
    // its own layer now, so it translates for free, and NOT re-uploading this big texture every frame
    // leaves the main thread to the scroll. Breathing freezes for the ~100ms of the scroll, unnoticed.
    let scrollUntil = 0;
    const onScroll = () => { scrollUntil = performance.now() + 100; };
    window.addEventListener("scroll", onScroll, { passive: true });
    // Idle freeze: ambient breathing keeps running for a few seconds after the last pointer activity,
    // then the loop parks itself and the surface holds its last frame. A pointer move revives it. This
    // is the whole power win — a page nobody is touching stops painting entirely.
    const IDLE_MS = 3000;

    const render = (now: number) => {
      if (performance.now() < scrollUntil) { raf = requestAnimationFrame(render); return; }
      const t = now / 1000;
      ctx.clearRect(0, 0, W, H);
      ctx.drawImage(base, 0, 0);

      const mouse = mouseRef.current;

      for (let k = 0; k < live.length; k++) {
        const i = live[k];
        const d = dots[i];
        const breathe = reduced ? 1 : 1 + 0.3 * Math.sin(t * 1.0 + jitter[i] * 6.28);
        let bulge = 0;
        if (mouse.inside) {
          const mx = d.x - mouse.x, my = d.y - mouse.y;
          const md = Math.sqrt(mx * mx + my * my);
          bulge = Math.exp(-Math.pow(md / REACH, FALLOFF));
        }
        const gauss = gaussBoost[i];
        const r = d.r * breathe * (OFF + gauss * (ON - OFF) + bulge * SWELL);
        paintDot(i, r, 1 + gauss * 0.25 + bulge * 0.22);
      }

      // Dead specks near the cursor still swell — they're baked flat, so redraw just the local few on
      // top. bulge only grows a dot, so the larger overdraw fully covers its resting print underneath.
      if (mouse.inside) {
        for (let i = 0; i < dots.length; i++) {
          if (gaussBoost[i] >= 0.08) continue;
          const d = dots[i];
          const mx = d.x - mouse.x, my = d.y - mouse.y;
          const md2 = mx * mx + my * my;
          if (md2 > (REACH * 2.2) * (REACH * 2.2)) continue; // outside any felt swell — leave the bake
          const bulge = Math.exp(-Math.pow(Math.sqrt(md2) / REACH, FALLOFF));
          if (bulge < 0.02) continue;
          paintDot(i, d.r * (OFF + bulge * SWELL), 1 + bulge * 0.22);
        }
      }

      // Dissolve the edge — erases the flesh and the dots, straight through to the abyss.
      ctx.globalCompositeOperation = "destination-out";
      ctx.drawImage(edgeMask, 0, 0, W, H);
      ctx.globalCompositeOperation = "source-over";

      // Park the loop when nothing needs to move: reduced motion (never breathes) or gone idle.
      const idle = performance.now() - activeRef.current > IDLE_MS;
      if (reduced || (idle && !mouse.inside)) { running = false; return; }
      raf = requestAnimationFrame(render);
    };

    const start = () => {
      if (running) return;
      running = true;
      raf = requestAnimationFrame(render);
    };
    startRef.current = start;

    // Only paint while the shoal is actually on screen. Scroll it away and the loop stops dead.
    const io = new IntersectionObserver(
      ([e]) => { if (e.isIntersecting) start(); else { running = false; cancelAnimationFrame(raf); } },
      { threshold: 0 },
    );
    if (wrapRef.current) io.observe(wrapRef.current);

    activeRef.current = performance.now(); // breathe on load, then park if untouched
    start();
    return () => { cancelAnimationFrame(raf); io.disconnect(); window.removeEventListener("scroll", onScroll); startRef.current = null; };
  }, [field, baked, reduced]);

  // Pointer handling. The wrapper is the layout box; the canvas is BLEED bigger on every side, so
  // wrapper coordinates need shifting into canvas space.
  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const rect = wrapRef.current!.getBoundingClientRect();
    const x = e.clientX - rect.left + BLEED;
    const y = e.clientY - rect.top + BLEED;
    mouseRef.current = { x, y, inside: true };
    activeRef.current = performance.now();
    startRef.current?.(); // revive the loop if it had parked itself while idle

    // hover name
    let best = -1, bestD = Infinity;
    for (let i = 0; i < field.memberPositions.length; i++) {
      const p = field.memberPositions[i];
      const dx = p.x - x, dy = p.y - y;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best >= 0 && bestD < 70 * 70) {
      const p = field.memberPositions[best];
      setHover({ i: best, x: p.x - BLEED, y: p.y - BLEED });
    } else setHover(null);
  };
  const onLeave = () => {
    mouseRef.current = { x: -9999, y: -9999, inside: false };
    setHover(null);
  };

  return (
    <div
      ref={wrapRef}
      onPointerMove={onMove}
      onPointerLeave={onLeave}
      className="relative w-full"
      style={{ height: 560 }}
    >
      {/*
       * Absolutely positioned and inset by -BLEED, so the canvas overhangs its slot on every side.
       * The layout box keeps its 560px height and nothing around it moves; the fade simply happens
       * off the edges of the box, over the abyss. pointer-events stay on the wrapper.
       */}
      <canvas
        ref={canvasRef}
        className="pointer-events-none absolute block"
        // Own compositor layer. This canvas is large and soft-edged (translucent rim), sitting over
        // the fixed abyss shader; without promotion the browser re-blends that whole alpha boundary
        // against the moving background on every scroll step — the vsync jank at the edges. Promoted,
        // scrolling just translates the finished layer.
        style={{ left: -BLEED, top: -BLEED, willChange: "transform" }}
      />

      {/*
       * Names, as DOM text — always present, never a hover-only reveal. Dark ink, because they sit
       * on lit flesh. Hovering a bloom swells its name the way the cursor swells the dots; nothing
       * appears or disappears. Never a number.
       */}
      {field.memberPositions.map((p, i) => {
        const isYou = p.m.id === you.id;
        const isHovered = hover?.i === i;
        return (
          <div
            key={p.m.id}
            className="pointer-events-none absolute -translate-x-1/2 -translate-y-1/2 whitespace-nowrap text-center"
            style={{
              // The name always sits on its own dark plate — a name has to be readable without
              // being hunted for, and dark ink on lit flesh loses to the dots underneath it.
              // Hovering only swells it and reveals the karma; it doesn't summon the plate.
              padding: isHovered ? "4px 14px" : "2px 8px",
              borderRadius: 999,
              background: "rgba(26, 16, 6, 0.82)",
              boxShadow: "0 18px 40px -14px rgba(0,0,0,0.55)",
              color: "var(--bone, #E6EDEC)",
              left: p.x - BLEED,
              top: p.y - BLEED,
              fontWeight: isYou ? 700 : 600,
              fontSize: isHovered ? 19 : 12,
              letterSpacing: "-0.01em",
              transition: "all 180ms cubic-bezier(.4,.14,.3,1)",
            }}
          >
            <div>
              {p.m.name}
              {isYou && !isHovered && (
                <span className="label ml-2 !text-[10px] !text-ink-mute">you</span>
              )}
            </div>
            {isHovered && (
              <div
                className="num"
                style={{
                  fontSize: 22,
                  fontWeight: 300,
                  marginTop: -1,
                  lineHeight: 1.05,
                  // Both readable on the dark plate. --umber (L=0.32) is invisible here; the sign
                  // and the depth of the warm tone carry the meaning, not raw darkness.
                  color: p.m.balance >= 0 ? "var(--gold-hi)" : "var(--bronze)",
                }}
              >
                {p.m.balance >= 0 ? "+" : "−"}
                {Math.abs(p.m.balance)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

