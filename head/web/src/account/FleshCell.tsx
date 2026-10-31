import { useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "../app/useReducedMotion";
import { valueNoise, paintFlesh, pigment, type Whiteness } from "../karma/skin";

// This page's own reading of the leucophore mottling — separate from Standings' WHITE so it can be tuned
// for a single large bloom without moving the field view. Start from the Standings values; adjust here.
// NB `scale`/`detail` are noise WAVELENGTHS, and valueNoise's lattice repeats every 16 units — keep the
// coarse `scale` small enough that px/scale sweeps well past 16 across the cell, or you sample one smooth
// hump and the whiteness piles into the middle. ~16 spreads it into several patches over the blob.
const WHITE: Whiteness = {
  rgb: "255, 246, 236",
  scale: 16,
  detail: 5.0,
  amount: 0.5,
  lo: 0.5,
  hi: 0.7,
  seed: 770,
};

// A dot field on a regular grid can't draw its randomness from one sequential LCG stream (skin's
// seededRand): consecutive draws lie on a lattice, and at a fixed stride per cell that lattice reads as
// diagonal stripes of same-sized dots. Instead hash each cell's (ix, iy) with a channel k — independent,
// position-keyed, no sequential correlation, so neighbours decorrelate and the banding goes.
function hash(ix: number, iy: number, k: number): number {
  let h = (ix * 374761393 + iy * 668265263 + k * 2246822519) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 1274126177) >>> 0;
  return ((h ^ (h >>> 16)) >>> 0) / 0xffffffff;
}

// A ROUND organic dissolve for the single bloom. Standings' buildEdgeMask is a superellipse (squareness
// 3.5) meant to tile a whole field of members; alone in a box its corners read as a rounded square. Here
// the distance is Euclidean (a true circle), warped by the same noise so the rim still breaks up
// organically. Black-alpha = how much to ERASE, used with destination-out to eat the flesh + dots together.
function buildBlobMask(w: number, h: number): HTMLCanvasElement {
  const scale = 3;
  const mw = Math.max(1, Math.ceil(w / scale)), mh = Math.max(1, Math.ceil(h / scale));
  const c = document.createElement("canvas");
  c.width = mw; c.height = mh;
  const ctx = c.getContext("2d")!;
  const img = ctx.createImageData(mw, mh);
  const noise = valueNoise(92);
  const smooth = (t: number) => t * t * (3 - 2 * t);
  const fadeFrom = 0.55, inset = 0.9, warp = 0.28, lobes = 26, ripples = 70;
  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      const nx = (x / mw) * 2 - 1, ny = (y / mh) * 2 - 1;
      let d = Math.hypot(nx, ny) / inset; // circular, not superelliptic
      const f = noise(x / lobes, y / lobes) * 0.7 + noise(x / ripples, y / ripples) * 0.3;
      d += (f - 0.5) * warp;
      const t = Math.max(0, Math.min(1, (d - fadeFrom) / (1 - fadeFrom)));
      const keep = 1 - smooth(t);
      img.data[(y * mw + x) * 4 + 3] = ((1 - keep) * 255) | 0;
    }
  }
  ctx.putImageData(img, 0, 0);
  return c;
}

/**
 * You, as the cluster sees you — one bloom of the Standings skin, singular and large. Same flesh and
 * pigment ramp (./karma/skin): a chromatophore bloom at centre, coloured by Balance (gold creditor →
 * umber debtor), in pale white-mottled leucophore flesh. Breathes, dissolves at its rim into an organic
 * blob, carries the Standings cursor swell.
 *
 * Bloom kept tight so the mottled flesh frames it. `balance` null (pre-Ph4, economy unwired) sits at
 * neutral bronze.
 */
const REACH = 50, SWELL = 0.8, FALLOFF = 1.5, OFF = 0.5, ON = 2.7;

// The central chromatophore bloom. RADIUS is its std as a fraction of min(W,H) — how big. POWER is the
// falloff steepness: 2 is a plain Gaussian; >2 flattens the core and sharpens the rim (a rounder disc),
// <2 makes it pointier with longer tails. w = exp(-0.5 · (r/σ)^POWER).
const BLOOM_RADIUS = 0.20, BLOOM_POWER = 3;

export function FleshCell({ balance }: { balance: number | null }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const mouseRef = useRef<{ x: number; y: number; inside: boolean }>({ x: -9999, y: -9999, inside: false });
  const activeRef = useRef(0);
  const startRef = useRef<(() => void) | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const reduced = useReducedMotion();

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

  // The dot field + its baked colours, regenerated per size or when the Balance (hence pigment) changes.
  // No BLEED here: unlike Standings the blob dissolves WITHIN its box, so it reads as one organic shape.
  const field = useMemo(() => {
    const W = Math.max(size.w, 1), H = Math.max(size.h, 1);
    const cx = W / 2, cy = H / 2;
    const sigma = Math.min(W, H) * BLOOM_RADIUS; // bloom std — tight, so the mottled flesh frames the pigment
    const step = 11;
    type D = { x: number; y: number; r: number; w: number; phase: number; col: [number, number, number] };
    const dots: D[] = [];
    const bal = balance ?? 0;
    let ix = 0;
    for (let x = step / 2; x < W - step / 2; x += step, ix++) {
      let iy = 0;
      for (let y = step / 2; y < H - step / 2; y += step, iy++) {
        if (hash(ix, iy, 0) > 0.94) continue;
        const jx = (hash(ix, iy, 1) - 0.5) * step * 0.85;
        const jy = (hash(ix, iy, 2) - 0.5) * step * 0.85;
        const px = x + jx, py = y + jy;
        const dx = px - cx, dy = py - cy;
        const w = Math.exp(-0.5 * Math.pow(Math.sqrt(dx * dx + dy * dy) / sigma, BLOOM_POWER));
        const jit = hash(ix, iy, 3);
        let col: [number, number, number];
        if (w < 0.08) {
          col = [0.02, 0.02, 0.03]; // black speckle on the bare flesh
        } else {
          const [pr, pg, pb] = pigment(bal, jit);
          const k = Math.min(1, w * 1.4);
          col = [pr * k + 0.02 * (1 - k), pg * k + 0.02 * (1 - k), pb * k + 0.03 * (1 - k)];
        }
        dots.push({ x: px, y: py, r: 1.15 + hash(ix, iy, 4) * 0.55, w, phase: hash(ix, iy, 5) * Math.PI * 2, col });
      }
    }
    return { W, H, dots };
  }, [size.w, size.h, balance]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !size.w || !size.h) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const { W, H, dots } = field;
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const edgeMask = buildBlobMask(W, H);
    const live = dots.filter((d) => d.w >= 0.08);
    const dead = dots.filter((d) => d.w < 0.08);

    // Resting dot size scales WITH the gaussian (∝ nearness to center): at w=1 it's ON, and it falls off
    // proportionally so 'off' dots far out shrink toward nothing instead of carpeting the cell at a flat
    // OFF floor. Near-center offs stay proportionally bigger; nothing far out reaches the core's size.
    const restSize = (w: number) => Math.pow(w, 0.3) * (OFF + w * (ON - OFF));

    const paint = (d: (typeof dots)[number], r: number, lift: number) => {
      ctx.fillStyle = `rgb(${(Math.min(1, d.col[0] * lift) * 255) | 0}, ${(Math.min(1, d.col[1] * lift) * 255) | 0}, ${(Math.min(1, d.col[2] * lift) * 255) | 0})`;
      ctx.beginPath();
      ctx.arc(d.x, d.y, r, 0, Math.PI * 2);
      ctx.fill();
    };

    // Static base: the white-mottled flesh + the dead specks at rest. Blitted each frame.
    const base = document.createElement("canvas");
    base.width = W; base.height = H;
    const bctx = base.getContext("2d")!;
    paintFlesh(bctx, W, H, WHITE);
    for (const d of dead) {
      bctx.fillStyle = `rgb(${(d.col[0] * 255) | 0}, ${(d.col[1] * 255) | 0}, ${(d.col[2] * 255) | 0})`;
      bctx.beginPath();
      bctx.arc(d.x, d.y, d.r * restSize(d.w), 0, Math.PI * 2);
      bctx.fill();
    }

    let raf = 0, running = false;
    const IDLE_MS = 3000;

    const render = (now: number) => {
      const t = now / 1000;
      const mouse = mouseRef.current;
      ctx.clearRect(0, 0, W, H);
      ctx.drawImage(base, 0, 0);

      for (let k = 0; k < live.length; k++) {
        const d = live[k];
        const breathe = reduced ? 1 : 1 + 0.3 * Math.sin(t + d.phase);
        let bulge = 0;
        if (mouse.inside) {
          const mx = d.x - mouse.x, my = d.y - mouse.y;
          bulge = Math.exp(-Math.pow(Math.sqrt(mx * mx + my * my) / REACH, FALLOFF));
        }
        paint(d, d.r * breathe * (restSize(d.w) + bulge * SWELL), 1 + d.w * 0.25 + bulge * 0.22);
      }

      // Dead specks near the cursor still swell — they're baked flat, so redraw just the local few on top.
      if (mouse.inside) {
        for (const d of dead) {
          const mx = d.x - mouse.x, my = d.y - mouse.y;
          const md2 = mx * mx + my * my;
          if (md2 > (REACH * 2.2) * (REACH * 2.2)) continue;
          const bulge = Math.exp(-Math.pow(Math.sqrt(md2) / REACH, FALLOFF));
          if (bulge < 0.02) continue;
          paint(d, d.r * (restSize(d.w) + bulge * SWELL), 1 + bulge * 0.22);
        }
      }

      ctx.globalCompositeOperation = "destination-out";
      ctx.drawImage(edgeMask, 0, 0, W, H);
      ctx.globalCompositeOperation = "source-over";

      if (reduced || (performance.now() - activeRef.current > IDLE_MS && !mouse.inside)) { running = false; return; }
      raf = requestAnimationFrame(render);
    };
    const start = () => { if (!running) { running = true; raf = requestAnimationFrame(render); } };
    startRef.current = start;

    const io = new IntersectionObserver(
      ([e]) => { if (e.isIntersecting) { activeRef.current = performance.now(); start(); } else { running = false; cancelAnimationFrame(raf); } },
      { threshold: 0 },
    );
    if (wrapRef.current) io.observe(wrapRef.current);
    activeRef.current = performance.now();
    start();
    return () => { cancelAnimationFrame(raf); io.disconnect(); startRef.current = null; };
  }, [field, reduced, size.w, size.h]);

  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const rect = wrapRef.current!.getBoundingClientRect();
    mouseRef.current = { x: e.clientX - rect.left, y: e.clientY - rect.top, inside: true };
    activeRef.current = performance.now();
    startRef.current?.(); // revive the loop if it had parked
  };
  const onLeave = () => {
    mouseRef.current = { x: -9999, y: -9999, inside: false };
  };

  return (
    <div ref={wrapRef} onPointerMove={onMove} onPointerLeave={onLeave} className="relative aspect-square w-full max-w-[20rem]">
      <canvas ref={canvasRef} className="pointer-events-none absolute left-0 top-0 block" style={{ willChange: "transform" }} />
    </div>
  );
}
