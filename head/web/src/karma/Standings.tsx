import { useEffect, useMemo, useRef, useState } from "react";
import { useReducedMotion } from "../app/useReducedMotion";
import { seededRand, buildEdgeMask, paintFlesh, pigment } from "./skin";

/** One person in the field. Adapted from api.ts's `Standing` by KarmaPage — this surface needs only
 *  who someone is and where they stand, and takes no interest in Share, nice, or their Escrow. */
export type Member = { id: number; name: string; balance: number };

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
 *
 * The flesh (fleshy skin + organic dissolving edge) and the pigment ramp both live in ./skin now, shared
 * with the Account hero; this file owns the dot FIELD — placement, breathing, the cursor wake.
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
  swell: 2.0,

  /**
   * How the swell falls off with distance. 2 is a plain gaussian — a tight bump with a short tail.
   * BELOW 2 the tail runs longer, so the swell fades out more gradually instead of stopping dead.
   */
  falloff: 1.0,

  /** Dot size when it belongs to nobody, and at a member's fully-expanded centre. */
  off: 0.4,
  on: 2.7,
  /** Max size (as the same unit as `off`/`on`) that an off dot may reach when the cursor is over it */
  offMax: 0.8,
};

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

/** `youId` is null for an Admin, who is outside the economy and so appears in nobody's field — the
 *  "you" marker simply never matches, which is the honest rendering of having no standing. */
export function Standings({ members, youId }: { members: Member[]; youId: number | null }) {
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
    const boxH = Math.max(size.h, 100);
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

    const bloom = Math.min(boxW, boxH) * 0.16;
    const memberPositions = members.map((m, i) => ({
      m,
      x: pts[i].x,
      y: pts[i].y,
      radius: bloom * (1.0 + rand2() * 0.8),
    }));

    return { dots, memberPositions, w, h };
    // `members` is a dependency now that it arrives from the Head: a User joining, or anyone's Balance
    // moving, re-places the field. The placement is seeded, so the same roster always lands the same way.
  }, [size.w, size.h, members]);

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
    const { jitter, colors, gaussBoost, ownerIdx } = baked;

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
          // scale the cursor bloom by the dot's gaussian weight so it follows the same falloff
          bulge *= gaussBoost[i];
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
          const bulgeLocal = Math.exp(-Math.pow(Math.sqrt(md2) / REACH, FALLOFF));
          if (bulgeLocal < 0.02) continue;
          // Off (dead) specks should be allowed to grow up to SKIN.offMax when the cursor is directly
          // over them. Do not scale this by gaussBoost (they are far from member centres by definition).
          const offMax = SKIN.offMax ?? OFF;
          const radiusMul = OFF + bulgeLocal * (offMax - OFF);
          paintDot(i, d.r * radiusMul, 1 + bulgeLocal * 0.22);
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
    if (best >= 0 && bestD < 500) {
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
      className="relative mx-auto"
      style={{ width: "70vw", height: "28vh" }}
    >
      <div
        className="absolute block"
        style={{ inset: -BLEED, pointerEvents: "auto" }}
        onPointerMove={onMove}
        onPointerLeave={onLeave}
      />

      {/*
       * Absolutely positioned and inset by -BLEED, so the canvas overhangs its slot on every side.
       * The layout box keeps its height and nothing around it moves; the fade simply happens
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
        const isYou = p.m.id === youId;
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
              // Invisible for the first second, then fade up onto the settled field. `both` holds the
              // opacity:0 start through the 1s delay. Skipped under reduced motion — no intro, just shown.
              animation: reduced ? undefined : "name-rise 400ms cubic-bezier(.4,.14,.3,1) 0.2s both",
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
