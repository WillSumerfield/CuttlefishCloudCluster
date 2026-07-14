import { useEffect, useMemo, useRef, useState } from "react";
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

// Alpha mask, built once per size at 1/3 resolution and scaled up (cheap, and the interpolation
// softens it further). Black pixels with alpha = how much to ERASE; used with `destination-out`.
function buildEdgeMask(w: number, h: number): HTMLCanvasElement {
  const scale = 3;
  const mw = Math.max(1, Math.ceil(w / scale)), mh = Math.max(1, Math.ceil(h / scale));
  const c = document.createElement("canvas");
  c.width = mw; c.height = mh;
  const ctx = c.getContext("2d")!;
  const img = ctx.createImageData(mw, mh);
  const noise = valueNoise(91);

  const WARP = 0.34;   // peak-to-peak noise displacement of the edge
  const INSET = 0.74;  // the organic edge sits at 74% of the half-size, not at the canvas rim

  for (let y = 0; y < mh; y++) {
    for (let x = 0; x < mw; x++) {
      const nx = (x / mw) * 2 - 1, ny = (y / mh) * 2 - 1;

      // superellipse distance: n=5 keeps the footprint roughly rectangular. Scaled by INSET so that
      // even where the noise pushes the edge outward, the fade still COMPLETES before the canvas
      // runs out — otherwise the rect clips a half-opaque edge and you get a hard straight cut.
      const n = 5;
      const ax = Math.abs(nx) / INSET, ay = Math.abs(ny) / INSET;
      let d = Math.pow(Math.pow(ax, n) + Math.pow(ay, n), 1 / n);

      // warp the edge with two octaves of noise so it is never a clean curve
      const f = noise(x / 26, y / 26) * 0.7 + noise(x / 11, y / 11) * 0.3;
      d += (f - 0.5) * WARP;

      // keep the core solid, dissolve the rim
      const t = Math.max(0, Math.min(1, (d - 0.62) / (1.0 - 0.62)));
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

// pigment colour for a member: umber (debt) → bronze (neutral) → gold (creditor)
function pigment(balance: number, jitter: number): [number, number, number] {
  // clamp balance to [-1, 1]
  const b = Math.max(-1, Math.min(1, balance));
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
  const wakeRef = useRef<WakePoint[]>([]);
  const mouseRef = useRef<{ x: number; y: number; inside: boolean }>({ x: -9999, y: -9999, inside: false });
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

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
    const step = 11;
    for (let y = step / 2; y < h - step / 2; y += step) {
      for (let x = step / 2; x < w - step / 2; x += step) {
        if (rand() > 0.94) continue; // a few gaps, so the grid never reads as a grid
        const jx = (rand() - 0.5) * step * 0.85;
        const jy = (rand() - 0.5) * step * 0.85;
        const r = 1.15 + rand() * 0.55;
        dots.push({ x: x + jx, y: y + jy, r, owner: -1 });
      }
    }

    // Members are placed across the VISIBLE box only — never out in the bleed, where they'd be
    // half-dissolved. Blooms scale with the box so they fill a wide monitor instead of getting lost.
    const rand2 = seededRand(90210);
    const memberPositions: { m: Member; x: number; y: number; radius: number }[] = [];
    const margin = 90;
    const minSpacing = Math.min(boxW / Math.sqrt(members.length), boxH) * 0.62;
    const bloom = Math.min(boxW, boxH) * 0.11;
    for (const m of members) {
      let placed: { x: number; y: number } | null = null;
      for (let attempt = 0; attempt < 600; attempt++) {
        const x = BLEED + margin + rand2() * (boxW - margin * 2);
        const y = BLEED + margin + rand2() * (boxH - margin * 2);
        let ok = true;
        for (const p of memberPositions) {
          const dx = p.x - x, dy = p.y - y;
          if (dx * dx + dy * dy < minSpacing * minSpacing) { ok = false; break; }
        }
        if (ok) { placed = { x, y }; break; }
      }
      if (!placed) placed = { x: w / 2, y: h / 2 };
      memberPositions.push({ m, x: placed.x, y: placed.y, radius: bloom * (0.85 + rand2() * 0.4) });
    }

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
        const bal = field.memberPositions[bestI].m.balance / 1000;
        const [pr, pg, pb] = pigment(bal, jitter[i]);
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

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size.w * dpr;
    canvas.height = size.h * dpr;
    canvas.style.width = size.w + "px";
    canvas.style.height = size.h + "px";
    ctx.scale(dpr, dpr);

    const edgeMask = buildEdgeMask(size.w, size.h);

    let raf = 0;
    const render = (now: number) => {
      const t = now / 1000;
      ctx.clearRect(0, 0, size.w, size.h);
      paintFlesh(ctx, size.w, size.h);

      // Purge stale wake points
      const life = 900; // ms
      const nowMs = now;
      const wake = wakeRef.current;
      while (wake.length && nowMs - wake[0].born > life) wake.shift();

      const dots = field.dots;
      const { jitter, colors, gaussBoost } = baked;
      const mouse = mouseRef.current;

      for (let i = 0; i < dots.length; i++) {
        const d = dots[i];

        // Wake displacement + boost
        let dx = 0, dy = 0, wakeBoost = 0;
        for (let wi = 0; wi < wake.length; wi++) {
          const wp = wake[wi];
          const age = (nowMs - wp.born) / life; // 0..1
          const decay = 1 - age;
          const ex = d.x - wp.x, ey = d.y - wp.y;
          const dist = Math.sqrt(ex * ex + ey * ey) + 0.001;
          const ring = 40 + age * 70;
          const band = Math.exp(-((dist - ring) * (dist - ring)) / (2 * 24 * 24));
          const push = band * decay * 1.8;
          dx += (ex / dist) * push;
          dy += (ey / dist) * push;
          wakeBoost = Math.max(wakeBoost, band * decay);
        }

        // Breathing pulse (per-dot phase from jitter)
        const breathe = 1 + 0.12 * Math.sin(t * 0.9 + jitter[i] * 6.28);

        // Cursor proximity bulge — much gentler
        let bulge = 0;
        if (mouse.inside) {
          const mx = d.x - mouse.x, my = d.y - mouse.y;
          const md = Math.sqrt(mx * mx + my * my);
          bulge = Math.exp(-(md * md) / (2 * 42 * 42)) * 0.28;
        }

        const gauss = gaussBoost[i]; // 0..1
        const r = d.r * breathe * (1 + gauss * 1.7 + bulge * 0.35 + wakeBoost * 0.25);

        // Colour: bake + slight brightening under wake/bulge/gauss
        const lift = 1 + gauss * 0.25 + bulge * 0.08 + wakeBoost * 0.15;
        const cr = Math.min(1, colors[i*3]     * lift);
        const cg = Math.min(1, colors[i*3 + 1] * lift);
        const cb = Math.min(1, colors[i*3 + 2] * lift);

        // Alpha: unused (dark) dots ride the fleshy skin softly;
        // member dots are more present.
        const alpha = 0.42 + gauss * 0.48 + bulge * 0.04 + wakeBoost * 0.08;

        ctx.fillStyle = `rgba(${(cr*255)|0}, ${(cg*255)|0}, ${(cb*255)|0}, ${alpha})`;
        ctx.beginPath();
        ctx.arc(d.x + dx, d.y + dy, r, 0, Math.PI * 2);
        ctx.fill();
      }

      // Names — Figtree, quiet, sits under each member's centre.
      // Bg is fleshy/light now — use deep ink for readability.
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (let mi = 0; mi < field.memberPositions.length; mi++) {
        const p = field.memberPositions[mi];
        const isYou = p.m.id === you.id;
        ctx.font = `${isYou ? 600 : 500} 12px Figtree, sans-serif`;
        ctx.fillStyle = isYou
          ? "rgba(30, 20, 10, 0.95)"
          : "rgba(40, 30, 20, 0.75)";
        ctx.fillText(p.m.name, p.x, p.y + p.radius * 1.15);
        if (isYou) {
          ctx.font = "600 9px Figtree, sans-serif";
          ctx.fillStyle = "rgba(30, 20, 10, 0.65)";
          ctx.fillText("YOU", p.x, p.y + p.radius * 1.15 + 15);
        }
      }

      // Dissolve the edge — erases flesh, dots and names together, straight through to the abyss.
      ctx.globalCompositeOperation = "destination-out";
      ctx.drawImage(edgeMask, 0, 0, size.w, size.h);
      ctx.globalCompositeOperation = "source-over";

      raf = requestAnimationFrame(render);
    };
    raf = requestAnimationFrame(render);
    return () => cancelAnimationFrame(raf);
  }, [field, baked, size.w, size.h]);

  // Pointer handling — sample into wake trail, and hit-test names.
  const lastSampleRef = useRef(0);
  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const rect = wrapRef.current!.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    mouseRef.current = { x, y, inside: true };
    const now = performance.now();
    // sample every ~28ms
    if (now - lastSampleRef.current > 28) {
      lastSampleRef.current = now;
      wakeRef.current.push({ x, y, born: now });
      if (wakeRef.current.length > 40) wakeRef.current.shift();
    }
    // hover name
    let best = -1, bestD = Infinity;
    for (let i = 0; i < field.memberPositions.length; i++) {
      const p = field.memberPositions[i];
      const dx = p.x - x, dy = p.y - y;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best >= 0 && bestD < 60 * 60) {
      const p = field.memberPositions[best];
      setHover({ i: best, x: p.x, y: p.y });
    } else setHover(null);
  };
  const onLeave = () => {
    mouseRef.current = { x: -9999, y: -9999, inside: false };
    setHover(null);
  };

  const hoveredName = hover ? field.memberPositions[hover.i].m.name : null;

  return (
    <div
      ref={wrapRef}
      onPointerMove={onMove}
      onPointerLeave={onLeave}
      className="relative w-full"
      style={{ height: 560 }}
    >
      <canvas ref={canvasRef} className="relative block h-full w-full" />

      {/* Hover badge (name only, never a number) */}
      {hoveredName && (
        <div
          className="pointer-events-none absolute -translate-x-1/2 -translate-y-full"
          style={{ left: hover!.x, top: hover!.y - 8 }}
        >
          <span className="rounded-[10px] bg-[color:var(--body)]/80 px-3 py-1 text-[12px] text-ink shadow-[0_10px_30px_-10px_rgba(0,0,0,0.7)]">
            {hoveredName}
          </span>
        </div>
      )}
    </div>
  );
}

