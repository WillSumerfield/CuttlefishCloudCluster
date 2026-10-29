import { useEffect, useMemo, useRef, useState } from "react";
import { edgeFade } from "../app/edgeFade";
import { useReducedMotion } from "../app/useReducedMotion";
import type { Fold } from "./fold";
import { ratioOf, tideOklch } from "./fold";
import { LEGEND_W, SEAM, SEAM_FADE, SEAM_FONT, SEAM_TICKS } from "./seam";

/**
 * The Tide — the page's signature, the water the whole metaphor points at. A day of mean Demand across
 * the fleet (0→23h), drawn as a lit waterline over a sinking iridescent body: teal where the cluster is
 * quiet, violet where it's fought over. The faint lines behind it are the individual Arms — highlight one
 * to read its name and its own multiplier. A marker sits on the current hour. The surface breathes like
 * moving water (the cluster working); reduced-motion stills it.
 *
 * Iridescence only — this is a machine fact. No borders. The body is faint at its waterline and grows
 * solid toward its foot, which is the seam with the Usage bands below (see seam.ts): hour labels are cut
 * out of the solid water there, and the last SEAM px fade out over the gold.
 */

const INK_SOFT = "oklch(0.78 0.015 90 / 0.4)"; // now-marker stem
const SEAM_WASH = "rgba(205,242,255,0.18)"; // light over the foot, so the knocked-out hours have contrast
const TIDE = {
  // Empty canvas above the water: the now-stem and tall Arm lines rise into it, and a mouse can approach a
  // high line from above (so hover reaches it). The demand band keeps its size — padT absorbs the headroom
  // and the wrapper grows by the same amount, so the waterline holds its screen spot once TidePage offsets
  // the section up by HEADROOM more. Change here → also bump wrapRef's h-[…] and TidePage's -mt-[…].
  headroom: 160,
  footDrop: 100,
  hoverRadius: 30,
  vFillPadRatio: 0.06,
  nowStem: {
    fadeToTop: 0.6,
    labelOffsetY: 80,
    stemTop: 50,
  },
  wave: {
    slowAmp: 5,
    slowFreq: 1.1,
    slowPhase: 0.6,
    fastAmp: 2,
    fastFreq: 1,
    fastPhase: 2,
  },
} as const;

const hourLabel = (h: number) =>
  h === 0 ? "12am" : (h === 12 || h === 24) ? "12pm" : h < 12 ? `${h}am` : `${h - 12}pm`;

const tideWaveOffset = (hf: number, t: number, reduced: boolean) => {
  if (reduced) return 0;
  return (
    Math.sin(t * TIDE.wave.slowFreq + hf * TIDE.wave.slowPhase) * TIDE.wave.slowAmp +
    Math.sin(t * TIDE.wave.fastFreq - hf * TIDE.wave.fastPhase) * TIDE.wave.fastAmp
  );
};

type Tip = { x: number; y: number; title: string; sub: string; color: string } | null;

export function TideHorizon({ fold }: { fold: Fold }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // The now-marker's own unmasked layer: the water's mask clears the right strip for the legend, and the
  // marker must still show when "now" is late in the day.
  const markRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const hoverArmRef = useRef(-1);
  const timeRef = useRef(0);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [tip, setTip] = useState<Tip>(null);
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

  // Geometry + the samplers, shared by the draw loop and the hover hit-test so they never disagree.
  const geom = useMemo(() => {
    const W = Math.max(size.w, 1), H = Math.max(size.h, 1);
    // No horizontal padding: the water spans the full box so edgeFade has real pixels to dissolve at the
    // sides (inset water would leave a near-hard edge just past its end). padT carries the headroom above
    // the water (see TIDE.headroom); padB is the solid foot down to the seam, so the canvas fills the box.
    const padL = 0, padR = 0, padT = 26 + TIDE.headroom, padB = TIDE.footDrop;
    const innerW = Math.max(1, W - padL - padR), innerH = Math.max(1, H - padT - padB);
    const yBottom = padT + innerH;
    const xForHour = (h: number) => padL + (h / 24) * innerW;

    const finite = [...fold.mean, ...fold.perArm.flatMap((a) => a.hours)].filter(Number.isFinite);
    const lo = finite.length ? Math.min(...finite) : 0;
    const hi = finite.length ? Math.max(...finite) : 1;
    const span = Math.max(hi - lo, 1e-6);
    const pad = span * TIDE.vFillPadRatio;
    const dMin = lo - pad;
    const dMax = hi + pad;
    const dSpan = Math.max(dMax - dMin, 1e-6);
    const yForDemand = (d: number) => padT + innerH * (1 - (d - dMin) / dSpan);

    // Inverse of xForHour (which spans 0→24 across innerW) — must use 24, not 23, or the sampled hour
    // drifts from the cursor (0 at the left, a full hour off at the right) and the hover misreads.
    const hourAtX = (x: number) => Math.max(0, Math.min(24, ((x - padL) / innerW) * 24));
    // Cyclic smooth read of a 24-bucket array at a fractional hour — the day wraps at midnight.
    const sampleAt = (arr: number[], hf: number) => {
      const i0 = Math.floor(hf) % 24, i1 = (i0 + 1) % 24, t = hf - Math.floor(hf), s = t * t * (3 - 2 * t);
      return arr[i0] + (arr[i1] - arr[i0]) * s;
    };
    return { W, H, padL, padR, padT, padB, innerW, innerH, yBottom, xForHour, yForDemand, hourAtX, sampleAt };
  }, [size.w, size.h, fold]);

  // Each Arm's overall ratio vs the day mean — a hot card runs violet, a cold one teal.
  const armRatio = useMemo(
    () => fold.perArm.map((a) => a.hours.reduce((n, v) => n + v, 0) / a.hours.length / fold.dayMean || 1),
    [fold],
  );

  const hoverRadius: number = TIDE.hoverRadius;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !size.w || !size.h) return;
    const ctx = canvas.getContext("2d");
    const mark = markRef.current;
    const mctx = mark?.getContext("2d");
    if (!ctx || !mark || !mctx) return;

    const { W, padL, padT, innerW, yBottom, xForHour, yForDemand, sampleAt } = geom;
    // Canvas ends exactly at the body's foot (yFoot) = the box's bottom = the seam.
    const canvasH = yBottom + TIDE.footDrop;
    const font = `${SEAM_FONT.weight} ${SEAM_FONT.size}px ${getComputedStyle(canvas).fontFamily}`;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = W * dpr; canvas.height = canvasH * dpr;
    canvas.style.width = W + "px"; canvas.style.height = canvasH + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    mark.width = W * dpr; mark.height = canvasH * dpr;
    mark.style.width = W + "px"; mark.style.height = canvasH + "px";
    mctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    const STEP = 0.1; // hours per subsample — denser points, smoother slope, still cheap
    const dayMean = fold.dayMean;

    const surfaceY = (hf: number, t: number) => {
      return yForDemand(sampleAt(fold.mean, hf)) + tideWaveOffset(hf, t, reduced);
    };

    const traceSmooth = (points: { x: number; y: number }[], start = true) => {
      if (points.length === 0) return;
      if (start) ctx.moveTo(points[0].x, points[0].y);
      if (points.length === 1) return;
      if (points.length === 2) {
        ctx.lineTo(points[1].x, points[1].y);
        return;
      }
      ctx.quadraticCurveTo(
        points[1].x,
        points[1].y,
        (points[1].x + points[2].x) / 2,
        (points[1].y + points[2].y) / 2,
      );
      for (let i = 2; i < points.length - 2; i++) {
        ctx.quadraticCurveTo(
          points[i].x,
          points[i].y,
          (points[i].x + points[i + 1].x) / 2,
          (points[i].y + points[i + 1].y) / 2,
        );
      }
      ctx.quadraticCurveTo(
        points[points.length - 2].x,
        points[points.length - 2].y,
        points[points.length - 1].x,
        points[points.length - 1].y,
      );
    };

    const samplePoints = (readY: (hf: number) => number) => {
      const points: { x: number; y: number }[] = [];
      for (let hf = 0; hf <= 24.0001; hf += STEP) points.push({ x: xForHour(hf), y: readY(hf) });
      const last = { x: xForHour(24), y: readY(24) };
      if (points.length === 0 || points[points.length - 1].x !== last.x) points.push(last);
      return points;
    };

    const drawArms = (hovered: number, t: number) => {
      fold.perArm.forEach((arm, i) => {
        const on = hovered === i;
        const alpha = hovered < 0 ? 0.16 : on ? 0.95 : 0.05;
        const lineY = (hf: number) => yForDemand(sampleAt(arm.hours, hf)) + tideWaveOffset(hf, t, reduced);
        ctx.strokeStyle = tideOklch(armRatio[i], alpha);
        ctx.lineWidth = on ? 1.8 : 1;
        ctx.shadowColor = on ? tideOklch(armRatio[i]) : "transparent";
        ctx.shadowBlur = on ? 10 : 0;
        ctx.beginPath();
        traceSmooth(samplePoints(lineY));
        ctx.stroke();
        ctx.shadowBlur = 0;
      });
    };

    const draw = (now: number) => {
      const t = now / 1000;
      timeRef.current = t;
      const hovered = hoverArmRef.current;
      ctx.clearRect(0, 0, W, canvasH);
      mctx.clearRect(0, 0, W, canvasH);
      const yFoot = yBottom + TIDE.footDrop;

      // 1 — the mean body: a filled area, iridescent by hour, sinking to the abyss at its foot.
      const body = samplePoints((hf) => surfaceY(hf, t));
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(padL, yFoot);
      ctx.lineTo(body[0].x, body[0].y);
      traceSmooth(body.slice(1), false);
      ctx.lineTo(padL + innerW, yFoot);
      ctx.closePath();
      ctx.clip();

      const horiz = ctx.createLinearGradient(padL, 0, padL + innerW, 0);
      for (let h = 0; h <= 23; h++) horiz.addColorStop(h / 23, tideOklch(fold.mean[h] / dayMean, 1));
      ctx.fillStyle = horiz;
      ctx.fillRect(padL, padT, innerW, yFoot - padT);

      // Faint at the waterline, solid at the foot: scale the body's alpha by a vertical ramp. The clip
      // keeps destination-in to the body, so nothing else on the canvas is touched.
      const ramp = ctx.createLinearGradient(0, padT, 0, yBottom);
      ramp.addColorStop(0, "rgba(0,0,0,0.22)");
      ramp.addColorStop(1, "rgba(0,0,0,1)");
      ctx.globalCompositeOperation = "destination-in";
      ctx.fillStyle = ramp;
      ctx.fillRect(padL, padT, innerW, yFoot - padT);
      ctx.globalCompositeOperation = "source-over";

      // Light over the foot — the seam is the brightest band on the page.
      // A broad rise that plateaus into the seam — no peak, or the peak reads as a stripe.
      const washTop = yFoot - SEAM - 70;
      const wash = ctx.createLinearGradient(0, washTop, 0, yFoot);
      wash.addColorStop(0, "rgba(205,242,255,0)");
      wash.addColorStop(0.75, SEAM_WASH);
      wash.addColorStop(1, SEAM_WASH);
      ctx.fillStyle = wash;
      ctx.fillRect(padL, washTop, innerW, yFoot - washTop);
      ctx.restore();

      // 2 — the Arms: faint over the water, or, when one is hovered, on top of everything.
      if (hovered < 0) drawArms(hovered, t);

      // 3 — the lit surface line, coloured per hour, dimmed while an Arm is soloed.
      ctx.lineWidth = 2;
      ctx.shadowBlur = 10;
      const lineAlpha = hovered < 0 ? 0.95 : 0.28;
      const grad = ctx.createLinearGradient(padL, 0, padL + innerW, 0);
      for (let h = 0; h <= 23; h++) grad.addColorStop(h / 23, tideOklch(fold.mean[h] / dayMean, lineAlpha));
      ctx.strokeStyle = grad;
      ctx.shadowColor = tideOklch(fold.mean[Math.round(fold.nowHour)] / dayMean, lineAlpha);
      ctx.beginPath();
      traceSmooth(body);
      ctx.stroke();
      ctx.shadowBlur = 0;

      // The side + foot dissolve is a CSS mask on the canvas element (edgeFade), applied below the surface
      // line but above the now-marker — which, with the tick row, is HTML so the fade never eats the labels.

      // 4 — the now-marker: a quiet stem up from the waterline, a lit bead where it meets the tide. The
      // stem rises from the canvas top through the headroom, fading in, so it reads however high the tide is.
      const nx = xForHour(fold.nowHour), ny = surfaceY(fold.nowHour, t);
      const stemGrad = mctx.createLinearGradient(0, TIDE.nowStem.stemTop, 0, ny);
      stemGrad.addColorStop(0, "rgba(0,0,0,0)");
      stemGrad.addColorStop(TIDE.nowStem.fadeToTop, INK_SOFT);
      stemGrad.addColorStop(1, INK_SOFT);
      mctx.strokeStyle = stemGrad;
      mctx.lineWidth = 1;
      mctx.beginPath(); mctx.moveTo(nx, TIDE.nowStem.stemTop); mctx.lineTo(nx, ny); mctx.stroke();
      const bead = tideOklch(fold.multiplier);
      mctx.fillStyle = bead; mctx.shadowColor = bead; mctx.shadowBlur = 12;
      mctx.beginPath(); mctx.arc(nx, ny, 3.5, 0, Math.PI * 2); mctx.fill();
      mctx.shadowBlur = 0;

      if (hovered >= 0) drawArms(hovered, t);

      // 5 — hours knocked out of the solid foot, just above the overlap with the gold.
      ctx.globalCompositeOperation = "destination-out";
      ctx.font = font;
      ctx.textAlign = "center";
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = "#000";
      for (const f of SEAM_TICKS) ctx.fillText(hourLabel(f * 24), xForHour(f * 24), yFoot - SEAM - 7);
      ctx.globalCompositeOperation = "source-over";

      if (!reduced) raf = requestAnimationFrame(draw);
    };

    let raf = 0;
    const io = new IntersectionObserver(
      ([e]) => {
        cancelAnimationFrame(raf);
        if (e.isIntersecting) raf = requestAnimationFrame(draw);
      },
      { threshold: 0 },
    );
    if (wrapRef.current) io.observe(wrapRef.current);
    raf = requestAnimationFrame(draw);
    return () => { cancelAnimationFrame(raf); io.disconnect(); };
  }, [geom, fold, reduced, armRatio, size.w, size.h]);

  const onMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const rect = wrapRef.current!.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const hf = geom.hourAtX(x);
    const hour = Math.round(hf);

    // Which Arm line is the cursor nearest? Pick it if within reach; else read the mean.
    const t = timeRef.current;
    let best = -1, bestDy = hoverRadius;
    fold.perArm.forEach((arm, i) => {
      const dy = Math.abs(geom.yForDemand(geom.sampleAt(arm.hours, hf)) + tideWaveOffset(hf, t, reduced) - y);
      if (dy < bestDy) { bestDy = dy; best = i; }
    });
    hoverArmRef.current = best;

    const val = best >= 0 ? geom.sampleAt(fold.perArm[best].hours, hf) : geom.sampleAt(fold.mean, hf);
    // Same helper the card's multiplier goes through — hovering the now-hour must not read a different
    // number than the card printing that same hour.
    const ratio = ratioOf(val, fold.dayMean);
    setTip({
      x, y,
      title: best >= 0 ? fold.perArm[best].node : `${ratio.toFixed(1)}×`,
      sub: best >= 0 ? `${ratio.toFixed(1)}× · ${hourLabel(hour)}` : hourLabel(hour),
      color: tideOklch(ratio),
    });
  };
  const onLeave = () => {
    hoverArmRef.current = -1;
    setTip(null);
  };

  // The "now" label rides as HTML above the masked canvas, so the edge-fade never touches it.
  const nowX = geom.xForHour(fold.nowHour);
  const nowRight = Math.abs(nowX - (geom.padL + geom.innerW)) < 40;

  return (
    <div
      ref={wrapRef}
      onPointerMove={onMove}
      onPointerLeave={onLeave}
      // Heights = base (16rem / 20rem) + TIDE.headroom (10rem) + the foot past the old tick row (66px).
      // Keep in sync with TIDE.headroom / footDrop.
      className="relative h-[calc(26rem+66px)] w-full md:h-[calc(30rem+66px)]"
    >
      {/* Sides dissolve; the foot fades out only over the SEAM overlap, where the gold shows through. */}
      <canvas ref={canvasRef} className="absolute left-0 top-0 block" style={edgeFade(0.2, (SEAM * SEAM_FADE) / Math.max(size.h, 1), LEGEND_W)} />
      <canvas ref={markRef} className="pointer-events-none absolute left-0 top-0 block" />
      {size.w > 0 && (
        <>
          <span
            className="pointer-events-none absolute text-[11px] font-medium text-ink-mute"
            style={{
              left: nowRight ? nowX - 6 : nowX + 6,
              top: TIDE.nowStem.stemTop + TIDE.nowStem.labelOffsetY - 11,
              transform: nowRight ? "translateX(-100%)" : undefined,
            }}
          >
            now
          </span>
        </>
      )}
      {tip && (
        <div
          className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-[8px] px-2.5 py-1.5"
          style={{
            left: tip.x,
            top: tip.y - 12,
            background: "color-mix(in oklab, var(--well) 82%, black)",
            boxShadow: "0 6px 24px rgba(0,0,0,0.4)",
          }}
        >
          <div className="num text-[13px]" style={{ color: tip.color }}>{tip.title}</div>
          <div className="text-[11px] text-ink-mute">{tip.sub}</div>
        </div>
      )}
    </div>
  );
}
