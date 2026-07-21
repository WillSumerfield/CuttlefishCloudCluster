import { useEffect, useRef } from "react";
import { useReducedMotion } from "../app/useReducedMotion";
import { fullscreenQuad, program } from "../shaders/gl";
import vsrc from "../shaders/fullscreen.vert?raw";
import noiseGlsl from "../shaders/noise.glsl?raw";
import iridFrag from "../shaders/irid.frag?raw";

/**
 * THE CHROMATIC BLOOM — waiting, with no known end. Cool masses (teal/cyan/violet) that bloom in and
 * fade out while swirling one way around each card's center; distinct from the cloud, which is dark,
 * directional, "working". The GLSL and the full look are documented in `../shaders/irid.frag`.
 *
 * DRAWN PER CARD, but not one WebGL context per card — browsers cap live contexts (~16) and drop the
 * oldest, so a deep queue would cascade into CONTEXT_LOST. Instead ONE shared context owns a scratch
 * canvas; every frame each waiting row re-renders the shader into that scratch at its own size with its
 * own seed uniform, then blits the result into its own cheap 2D <canvas> (`drawImage`). Cost is O(1)
 * contexts for any queue depth, and each card's `seed` gives it independent water — neighbouring rows
 * never line up. The whole thing is screen-blended on the card's black plate, so troughs stay invisible.
 */

/**
 * THE ONLY THINGS WORTH TUNING. Pushed to the shader as uniforms, so edits hot-reload without a
 * recompile. Colours are linear RGB 0..1, on black — brighter than the ground: this is a signal.
 * (Mass COUNT is a compile-time #define in irid.frag, not here.)
 */
const IRID = {
  /** Swirl speed — scales every mass's orbit and drift. Faster than the ground; meant to be noticed. */
  speed: 1.5,
  /** Warp feature size. LOWER = broader inky edges on the masses. */
  scale: 2.0,
  /** How hard masses are pushed off their clean circles into wandering, inky shapes. */
  warp: 4.0,
  /** Fine detail on the warp. */
  roughness: 0.2,
  /** Overall brightness of the lit masses — the one dial for "too dim / too hot". Black stays black. */
  gain: 0.8,

  /** Per-axis orbit speed. Raise one above the other so masses sweep faster horizontally than vertically. */
  speedX: 1.0,
  speedY: 1.2,
  /** Orbit half-extent per axis (in uv, edge = 0.5). Wider X fills the row; the ratio also sets how much
   *  horizontal vs vertical travel there is. */
  orbitX: 0.5,
  orbitY: 0.5,
  /** Orbit shape. 0 = round orbit · 1 = hard rectangle hugging the card edges. */
  square: 0.2,

  /** Cool ramp, linear RGB, on black. Masses cycle through these in order. */
  teal: [0.02, 0.22, 0.2], //   --irid-low
  cyan: [0.04, 0.22, 0.34], //  --irid-mid
  violet: [0.16, 0.07, 0.34], // --irid-high
};

// Cap at 2× device pixels: these per-card canvases are small, so full-DPR rendering is cheap, and a lower
// cap upscales the render (visible interpolation lines through the bloom's smooth gradients).
const dpr = () => Math.min(window.devicePixelRatio || 1, 2);

/* ─────────────────── the one shared context + scratch canvas ─────────────────── */

type Card = { blit: () => void };
const cards = new Set<Card>();

let scratch: HTMLCanvasElement | null = null;
let sgl: WebGLRenderingContext | null = null;
let uRes: WebGLUniformLocation | null = null;
let uTime: WebGLUniformLocation | null = null;
let uSeed: WebGLUniformLocation | null = null;
let sStart = 0;
let sFailed = false;
let raf = 0;
let listening = false;
let clockT = 0; // shared time (seconds); 0 and frozen under reduced motion

function initScratch() {
  if (scratch || sFailed) return;
  const c = document.createElement("canvas");
  const gl = c.getContext("webgl", { antialias: false, premultipliedAlpha: false, alpha: false, preserveDrawingBuffer: true });
  if (!gl) {
    sFailed = true;
    return;
  }
  try {
    const prog = program(gl, vsrc, `${noiseGlsl}\n${iridFrag}`);
    fullscreenQuad(gl, prog);
    const u = (n: string) => gl.getUniformLocation(prog, n);
    // Static dials — set once.
    gl.uniform1f(u("uSpeed"), IRID.speed);
    gl.uniform1f(u("uScale"), IRID.scale);
    gl.uniform1f(u("uWarp"), IRID.warp);
    gl.uniform1f(u("uRough"), IRID.roughness);
    gl.uniform1f(u("uGain"), IRID.gain);
    gl.uniform1f(u("uSpeedX"), IRID.speedX);
    gl.uniform1f(u("uSpeedY"), IRID.speedY);
    gl.uniform1f(u("uOrbitX"), IRID.orbitX);
    gl.uniform1f(u("uOrbitY"), IRID.orbitY);
    gl.uniform1f(u("uSquare"), IRID.square);
    gl.uniform3fv(u("uTeal"), IRID.teal);
    gl.uniform3fv(u("uCyan"), IRID.cyan);
    gl.uniform3fv(u("uViolet"), IRID.violet);
    uRes = u("uRes");
    uTime = u("uTime");
    uSeed = u("uSeed");
    scratch = c;
    sgl = gl;
    sStart = performance.now();
  } catch (e) {
    sFailed = true;
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    console.warn("IridBloom: shader failed, waiting cards fall back to plate.", e);
  }
}

// Render one card's frame into the shared scratch. Scratch tracks card size; since every waiting card is
// the same size, this reallocs at most once and then just redraws.
function renderScratch(w: number, h: number, seed: number) {
  const gl = sgl!;
  if (scratch!.width !== w || scratch!.height !== h) {
    scratch!.width = w;
    scratch!.height = h;
    gl.viewport(0, 0, w, h);
  }
  gl.uniform2f(uRes, w, h);
  gl.uniform1f(uSeed, seed);
  gl.uniform1f(uTime, clockT);
  gl.drawArrays(gl.TRIANGLES, 0, 6);
}

function frame() {
  clockT = (performance.now() - sStart) / 1000;
  cards.forEach((c) => c.blit());
  raf = requestAnimationFrame(frame);
}

// Reduced motion holds clockT at 0, so content is static and position-independent — no scroll re-blit is
// needed (unlike the old shared field). Only a resize can change a card's size, so re-blit all on resize.
const onResize = () => cards.forEach((c) => c.blit());

function teardownIfIdle() {
  if (cards.size > 0 || !scratch) return;
  cancelAnimationFrame(raf);
  raf = 0;
  if (listening) {
    window.removeEventListener("resize", onResize);
    listening = false;
  }
  sgl?.getExtension("WEBGL_lose_context")?.loseContext();
  scratch = null;
  sgl = null;
}

/* ───────────────────────────── the per-card window ───────────────────────────── */

export function IridBloom() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const reduced = useReducedMotion();
  // A fixed, per-card random seed. Decorrelates this row's water from every other, and never re-rolls
  // (a re-roll each frame would make the whole card jump).
  const seed = useRef(Math.random() * 1000).current;

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    const blit = () => {
      initScratch();
      if (!scratch || sFailed) return;
      const d = dpr();
      const w = Math.max(1, Math.round(cv.clientWidth * d));
      const h = Math.max(1, Math.round(cv.clientHeight * d));
      if (cv.width !== w || cv.height !== h) {
        cv.width = w;
        cv.height = h;
      }
      renderScratch(w, h, seed);
      ctx.clearRect(0, 0, w, h);
      ctx.drawImage(scratch, 0, 0, w, h, 0, 0, w, h);
    };

    const card: Card = { blit };
    cards.add(card);

    if (reduced) {
      // Static: paint once, and again only if a resize changes the card's size.
      if (!listening) {
        window.addEventListener("resize", onResize);
        listening = true;
      }
      blit();
    } else if (!raf) {
      raf = requestAnimationFrame(frame);
    }

    return () => {
      cards.delete(card);
      teardownIfIdle();
    };
  }, [reduced, seed]);

  return (
    <canvas
      ref={ref}
      aria-hidden
      className="edge-fade pointer-events-none absolute inset-0 h-full w-full rounded-[inherit]"
      style={{ zIndex: 0, mixBlendMode: "screen" }}
    />
  );
}
