import { useEffect, useRef } from "react";
import { useReducedMotion } from "../app/useReducedMotion";

/**
 * THE CHROMATIC BLOOM — waiting, with no known end. Same METHOD as the ground water (`AbyssBackground`):
 * a fbm domain-warped WebGL flow, because that organic current is the point and CSS gradients can only
 * fake it as sliding shapes (three tries proved it). Recoloured to the COOL ramp (teal → cyan → violet,
 * "machine, waiting") and rendered on BLACK so each card `screen`-blends it — dark troughs add nothing,
 * only the bright currents glow through. Distinct from the cloud: that is dark, directional, "working".
 *
 * ONE shared GL context, not one per card. Browsers cap live contexts (~16) and lose them under churn —
 * per-card canvases went straight to CONTEXT_LOST. So a single hidden field canvas renders the flow at
 * viewport size, and every waiting row is a cheap 2D <canvas> that blits its own screen-rect out of the
 * field (`drawImage`). Cost is O(1) contexts for any queue depth, and because each row samples its true
 * viewport position the current is continuous from card to card — the rows are windows onto one field.
 */

const IRID = {
  /** Current speed. Faster than the ground — this one is meant to be noticed, a little. */
  speed: 0.16,
  /** Feature size. LOWER = broader masses. */
  scale: 2.6,
  /** How hard noise is stretched into currents. */
  warp: 2.4,
  /** Fine detail on the big shapes. */
  roughness: 0.34,

  /** Where the water sits vs black. A wide dark gap keeps the glow to bright filaments, not a full wash. */
  waterFrom: 0.34,
  waterTo: 1.02,

  /** Cool ramp, linear RGB, on black. Brighter than the ground: this is a signal, not the floor. */
  teal: [0.02, 0.22, 0.2], //   --irid-low
  cyan: [0.04, 0.22, 0.34], //  --irid-mid
  violet: [0.16, 0.07, 0.34], // --irid-high
  lift: [0.24, 0.46, 0.56], //  crests, where the current catches the light
};

const g = (n: number) => n.toFixed(4);
const v3 = (c: number[]) => `vec3(${g(c[0])}, ${g(c[1])}, ${g(c[2])})`;
const dpr = () => Math.min(window.devicePixelRatio || 1, 1.5);

/* ─────────────────────────── the one shared field ─────────────────────────── */

type Blit = () => void;
const blits = new Set<Blit>();

let fieldCanvas: HTMLCanvasElement | null = null;
let fieldGL: WebGLRenderingContext | null = null;
let fieldURes: WebGLUniformLocation | null = null;
let fieldUTime: WebGLUniformLocation | null = null;
let fieldStart = 0;
let fieldRaf = 0;
let fieldReduced = false;
let fieldFailed = false;
let fieldListening = false;

function renderField() {
  const gl = fieldGL, c = fieldCanvas;
  if (!gl || !c) return;
  const d = dpr();
  const w = Math.max(1, Math.floor(window.innerWidth * d));
  const h = Math.max(1, Math.floor(window.innerHeight * d));
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; gl.viewport(0, 0, w, h); }
  gl.uniform2f(fieldURes, c.width, c.height);
  gl.uniform1f(fieldUTime, (performance.now() - fieldStart) / 1000);
  gl.drawArrays(gl.TRIANGLES, 0, 6);
}

const runBlits = () => blits.forEach((b) => b());

function frame() {
  renderField();
  runBlits();
  fieldRaf = requestAnimationFrame(frame);
}

// Reduced motion: the field is static, but rows still move under it as the page scrolls — so re-blit on
// scroll (no re-render needed; preserveDrawingBuffer keeps the last frame) and re-render on resize.
const onScroll = () => runBlits();
const onResize = () => { renderField(); runBlits(); };

function initField(reduced: boolean) {
  if (fieldCanvas || fieldFailed) return;
  const c = document.createElement("canvas");
  const gl = c.getContext("webgl", { antialias: false, premultipliedAlpha: false, alpha: false, preserveDrawingBuffer: true });
  if (!gl) { fieldFailed = true; return; }

  try {
    const vsrc = `attribute vec2 a; void main(){ gl_Position = vec4(a, 0.0, 1.0); }`;
    const fsrc = `
      precision highp float;
      uniform vec2 uRes;
      uniform float uTime;

      float hash(vec2 p){ p = fract(p*vec2(123.34, 456.21)); p += dot(p, p+45.32); return fract(p.x*p.y); }
      float noise(vec2 p){
        vec2 i = floor(p); vec2 f = fract(p);
        float a = hash(i), b = hash(i+vec2(1.0,0.0)), c = hash(i+vec2(0.0,1.0)), d = hash(i+vec2(1.0,1.0));
        vec2 u = f*f*(3.0-2.0*f);
        return mix(mix(a,b,u.x), mix(c,d,u.x), u.y);
      }
      float fbm(vec2 p){
        float v = 0.0; float amp = 0.5;
        for(int i=0;i<4;i++){ v += amp*noise(p); p *= 1.92; amp *= ${g(IRID.roughness)}; }
        return v;
      }

      void main(){
        vec2 uv = gl_FragCoord.xy / uRes.xy;
        vec2 p = uv * vec2(uRes.x/uRes.y, 1.0) * ${g(IRID.scale)};
        float t = uTime * ${g(IRID.speed)};

        vec2 q = vec2(fbm(p + vec2(t*0.6, t*0.8)), fbm(p + vec2(5.2 - t*0.5, -t*0.7)));
        vec2 r = vec2(fbm(p + ${g(IRID.warp)}*q + vec2(1.7 + t*1.1, 9.2 - t*0.4)),
                      fbm(p + ${g(IRID.warp)}*q + vec2(8.3 - t*0.9, 2.8 + t*1.2)));
        float f = fbm(p + ${g(IRID.warp * 0.9)}*r + vec2(t*0.3, -t*0.2));

        float hue = fbm(p * 0.5 + vec2(-t*0.22, t*0.16));
        hue = smoothstep(0.12, 0.9, hue);
        float h1 = clamp(hue * 2.0, 0.0, 1.0);
        float h2 = clamp((hue - 0.5) * 2.0, 0.0, 1.0);
        vec3 cur = mix(mix(${v3(IRID.teal)}, ${v3(IRID.cyan)}, h1),
                       mix(${v3(IRID.cyan)}, ${v3(IRID.violet)}, h2), step(0.5, hue));

        vec3 col = mix(vec3(0.0), cur, smoothstep(${g(IRID.waterFrom)}, ${g(IRID.waterTo)}, f));
        col = mix(col, ${v3(IRID.lift)}, smoothstep(0.55, 1.08, f*f));

        col += (hash(gl_FragCoord.xy) - 0.5) / 255.0;
        gl_FragColor = vec4(col, 1.0);
      }
    `;
    const compile = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "compile failed");
      return s;
    };
    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, vsrc));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fsrc));
    gl.linkProgram(prog);
    gl.useProgram(prog);

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]), gl.STATIC_DRAW);
    const aLoc = gl.getAttribLocation(prog, "a");
    gl.enableVertexAttribArray(aLoc);
    gl.vertexAttribPointer(aLoc, 2, gl.FLOAT, false, 0, 0);

    fieldURes = gl.getUniformLocation(prog, "uRes");
    fieldUTime = gl.getUniformLocation(prog, "uTime");
    fieldCanvas = c;
    fieldGL = gl;
    fieldReduced = reduced;
    fieldStart = performance.now();

    if (reduced) {
      renderField();
      window.addEventListener("scroll", onScroll, { passive: true, capture: true });
      window.addEventListener("resize", onResize);
      fieldListening = true;
    } else {
      frame();
    }
  } catch (e) {
    fieldFailed = true;
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    console.warn("IridBloom: field shader failed, waiting cards fall back to plate.", e);
  }
}

function teardownFieldIfIdle() {
  if (blits.size > 0 || !fieldCanvas) return;
  cancelAnimationFrame(fieldRaf);
  fieldRaf = 0;
  if (fieldListening) {
    window.removeEventListener("scroll", onScroll, { capture: true } as EventListenerOptions);
    window.removeEventListener("resize", onResize);
    fieldListening = false;
  }
  fieldGL?.getExtension("WEBGL_lose_context")?.loseContext();
  fieldCanvas = null;
  fieldGL = null;
}

/* ───────────────────────────── the per-card window ───────────────────────────── */

export function IridBloom() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext("2d");
    if (!ctx) return;

    initField(reduced);

    // Blit this card's viewport rect out of the shared field, so the current lines up across rows.
    const blit = () => {
      const src = fieldCanvas;
      if (!src || src.width === 0) return;
      const d = dpr();
      const w = Math.max(1, Math.round(cv.clientWidth * d));
      const h = Math.max(1, Math.round(cv.clientHeight * d));
      if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
      const r = cv.getBoundingClientRect();
      ctx.clearRect(0, 0, w, h);
      ctx.drawImage(src, r.left * d, r.top * d, r.width * d, r.height * d, 0, 0, w, h);
    };
    blits.add(blit);
    blit(); // paint immediately, don't wait a frame (matters most under reduced motion)

    return () => {
      blits.delete(blit);
      teardownFieldIfIdle();
    };
  }, [reduced]);

  return (
    <canvas
      ref={ref}
      aria-hidden
      className="pointer-events-none absolute inset-0 h-full w-full rounded-[inherit]"
      style={{ zIndex: 0, mixBlendMode: "screen" }}
    />
  );
}
