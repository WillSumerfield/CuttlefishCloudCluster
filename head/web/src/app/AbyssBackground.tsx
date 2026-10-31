import { useEffect, useRef } from "react";
import { useReducedMotion } from "./useReducedMotion";
import { fullscreenQuad, program } from "../shaders/gl";
import vsrc from "../shaders/fullscreen.vert?raw";
import noiseGlsl from "../shaders/noise.glsl?raw";
import abyssFrag from "../shaders/abyss.frag?raw";

/**
 * Fullscreen WebGL fragment shader: slow-swirling deep water, layered fbm domain-warped noise. No text,
 * no UI, just ground. Fixed behind the page, pointer-events: none.
 *
 * THE GROUND IS NOT INTERFACE. It signals nothing, which is the only reason it's allowed to be coloured
 * and move at all (colour is otherwise signal, never decoration) — earned by staying slow, dark,
 * low-contrast, so it never competes with motion that IS signal (the cloud, the bloom). If it ever reads
 * as meaningful, turn the ground DOWN, not the signal up.
 *
 * Mounted once, above the router, so the water is continuous across pages. GLSL lives in
 * `../shaders/abyss.frag` (+ shared `noise.glsl`); dials here are pushed as uniforms, hot-reloading
 * without a recompile.
 */

/**
 * THE ONLY THINGS WORTH TUNING. Edit, save, watch.
 * Colours are linear RGB 0..1 and are DARK by design; the page's surfaces must stay above them.
 */
const WATER = {
  /** How fast the currents move. 0.16 = slow drift · 0.4 = busy · 0 = frozen. */
  speed: 0.175,

  /** Feature size. LOWER = bigger, broader masses. Higher = smaller, busier. */
  scale: 2.0,

  /** How hard the noise is stretched into swirls. HIGH = thin wiry filaments, low = soft blobs. */
  warp: 2.0,

  /** Fine detail. Higher = more small wisps riding on the big shapes. 0.5 = classic fbm. */
  roughness: 0.2,

  /** Where the green→blue mix sits. RAISE BOTH for more green; lower both for more blue. */
  hueFrom: 0.1,
  hueTo: 0.5,

  /** How much of the frame is lit current vs black. Widen the gap for a softer, mistier fade. */
  waterFrom: 0.04,
  waterTo: 0.74,

  /** Palette, darkest to brightest. Scale all four together to change overall brightness. */
  ink: [0.006, 0.028, 0.03], //  deepest water, nearly black
  green: [0.014, 0.14, 0.096], //  the green current
  blue: [0.016, 0.082, 0.155], //  the blue current
  lift: [0.08, 0.245, 0.205], //  the crests, where light reaches

  /** How much the edges of the screen darken, so the cards read cleanly. 0 = none, 1 = heavy. */
  vignette: 0.45,
};

export function AbyssBackground() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const gl = canvas.getContext("webgl", { antialias: false, premultipliedAlpha: false });
    if (!gl) return;

    let prog: WebGLProgram;
    try {
      prog = program(gl, vsrc, `${noiseGlsl}\n${abyssFrag}`);
    } catch (e) {
      // A failed shader is otherwise indistinguishable from a working one on a black page.
      console.warn("AbyssBackground: shader failed, ground stays black.", e);
      return;
    }
    fullscreenQuad(gl, prog);

    // Static dials — set once. Only uRes/uTime change after this.
    const u = (n: string) => gl.getUniformLocation(prog, n);
    gl.uniform1f(u("uSpeed"), WATER.speed);
    gl.uniform1f(u("uScale"), WATER.scale);
    gl.uniform1f(u("uWarp"), WATER.warp);
    gl.uniform1f(u("uRough"), WATER.roughness);
    gl.uniform1f(u("uHueFrom"), WATER.hueFrom);
    gl.uniform1f(u("uHueTo"), WATER.hueTo);
    gl.uniform1f(u("uWaterFrom"), WATER.waterFrom);
    gl.uniform1f(u("uWaterTo"), WATER.waterTo);
    gl.uniform1f(u("uVignette"), WATER.vignette);
    gl.uniform3fv(u("uInk"), WATER.ink);
    gl.uniform3fv(u("uGreen"), WATER.green);
    gl.uniform3fv(u("uBlue"), WATER.blue);
    gl.uniform3fv(u("uLift"), WATER.lift);
    const uRes = u("uRes");
    const uTime = u("uTime");

    let raf = 0;
    const start = performance.now();
    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const w = Math.floor(canvas.clientWidth * dpr);
      const h = Math.floor(canvas.clientHeight * dpr);
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
        gl.viewport(0, 0, w, h);
      }
      gl.uniform2f(uRes, canvas.width, canvas.height);
    };
    // Reduced motion: draw one frame and stop. Still water, not no water — the ground carries no
    // meaning, so freezing it costs nothing but the drift.
    const draw = (t: number) => {
      resize();
      gl.uniform1f(uTime, t);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    };
    const render = () => {
      draw((performance.now() - start) / 1000);
      raf = requestAnimationFrame(render);
    };
    if (reduced) draw(0);
    else render();

    // A frozen shader still has to repaint on resize, or the canvas rescales to garbage.
    const onResize = () => (reduced ? draw(0) : resize());
    window.addEventListener("resize", onResize);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
    };
  }, [reduced]);

  return (
    <canvas
      ref={ref}
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10 h-full w-full"
    />
  );
}
