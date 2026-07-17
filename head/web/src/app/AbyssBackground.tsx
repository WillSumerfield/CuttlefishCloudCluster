import { useEffect, useRef } from "react";
import { useReducedMotion } from "./useReducedMotion";

/**
 * Fullscreen WebGL fragment shader. Slow-swirling deep water — blue-green ink,
 * layered fbm domain-warped noise. No text, no ui — just ground.
 * Fixed behind the page; pointer-events: none.
 *
 * THE GROUND IS NOT INTERFACE. It signals nothing, which is the only reason it is allowed to be
 * coloured and to move at all (colour is otherwise signal, never decoration). It earns that exemption
 * by staying slow, dark and low-contrast — out of focus, behind the surfaces — so it can never be
 * mistaken for a signal and never competes with the motion that IS one (the cloud, the bloom). If it
 * ever starts reading as meaningful, turn the ground DOWN; do not turn the signal up.
 *
 * Mounted once, above the router: the water is continuous as you move between pages.
 */

/**
 * THE ONLY THINGS WORTH TUNING. Edit, save, watch — the shader recompiles on hot reload.
 * Colours are linear RGB 0..1 and are DARK by design; the page's surfaces must stay above them.
 */
const WATER = {
  /** How fast the currents move. 0.16 = slow drift · 0.4 = busy · 0 = frozen. */
  speed: 0.10,

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

// GLSL has no implicit int→float: every injected number must carry a decimal point.
const g = (n: number) => n.toFixed(4);
const v3 = (c: number[]) => `vec3(${g(c[0])}, ${g(c[1])}, ${g(c[2])})`;

export function AbyssBackground() {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const gl = canvas.getContext("webgl", { antialias: false, premultipliedAlpha: false });
    if (!gl) return;

    const vsrc = `
      attribute vec2 a;
      void main() { gl_Position = vec4(a, 0.0, 1.0); }
    `;
    const fsrc = `
      precision highp float;
      uniform vec2 uRes;
      uniform float uTime;

      // hash / value noise
      float hash(vec2 p){ p = fract(p*vec2(123.34, 456.21)); p += dot(p, p+45.32); return fract(p.x*p.y); }
      float noise(vec2 p){
        vec2 i = floor(p); vec2 f = fract(p);
        float a = hash(i);
        float b = hash(i+vec2(1.0,0.0));
        float c = hash(i+vec2(0.0,1.0));
        float d = hash(i+vec2(1.0,1.0));
        vec2 u = f*f*(3.0-2.0*f);
        return mix(mix(a,b,u.x), mix(c,d,u.x), u.y);
      }
      // 4 octaves; a fast-decaying amplitude gives broad masses rather than thin filaments.
      float fbm(vec2 p){
        float v = 0.0; float amp = 0.5;
        for(int i=0;i<4;i++){ v += amp*noise(p); p *= 1.92; amp *= ${g(WATER.roughness)}; }
        return v;
      }

      void main(){
        vec2 uv = gl_FragCoord.xy / uRes.xy;
        vec2 p = uv * vec2(uRes.x/uRes.y, 1.0) * ${g(WATER.scale)};
        float t = uTime * ${g(WATER.speed)};

        // domain warp — this is what turns noise into currents
        vec2 q = vec2(fbm(p + vec2(t*0.6, t*0.8)), fbm(p + vec2(5.2 - t*0.5, -t*0.7)));
        vec2 r = vec2(fbm(p + ${g(WATER.warp)}*q + vec2(1.7 + t*1.1, 9.2 - t*0.4)),
                      fbm(p + ${g(WATER.warp)}*q + vec2(8.3 - t*0.9, 2.8 + t*1.2)));
        float f = fbm(p + ${g(WATER.warp * 0.9)}*r + vec2(t*0.3, -t*0.2));

        // A SECOND, slower field decides the HUE of the water here — so green currents and blue
        // currents drift through each other independently of where the light/dark bands are.
        float hue = fbm(p * 0.45 + vec2(-t*0.22, t*0.17));
        hue = smoothstep(${g(WATER.hueFrom)}, ${g(WATER.hueTo)}, hue);

        vec3 current = mix(${v3(WATER.green)}, ${v3(WATER.blue)}, hue);
        vec3 col = mix(${v3(WATER.ink)}, current, smoothstep(${g(WATER.waterFrom)}, ${g(WATER.waterTo)}, f));
        col = mix(col, ${v3(WATER.lift)}, smoothstep(0.52, 1.05, f * f));

        // very faint warm undertone in the deepest currents — never red
        col += 0.010 * vec3(0.7, 0.55, 0.30) * smoothstep(0.72, 1.0, r.x);

        // slow vignette so surfaces read cleaner
        float d = distance(uv, vec2(0.5, 0.42));
        col *= 1.0 - smoothstep(0.35, 1.05, d) * ${g(WATER.vignette)};

        // dither to kill banding
        float dither = (hash(gl_FragCoord.xy) - 0.5) / 255.0;
        col += dither;

        gl_FragColor = vec4(col, 1.0);
      }
    `;

    // A failed shader is otherwise indistinguishable from a working one on a black page.
    const compile = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "shader compile failed");
      return s;
    };
    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, vsrc));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fsrc));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? "shader link failed");
    gl.useProgram(prog);

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1,  -1,1, 1,-1, 1,1]), gl.STATIC_DRAW);
    const aLoc = gl.getAttribLocation(prog, "a");
    gl.enableVertexAttribArray(aLoc);
    gl.vertexAttribPointer(aLoc, 2, gl.FLOAT, false, 0, 0);

    const uRes = gl.getUniformLocation(prog, "uRes");
    const uTime = gl.getUniformLocation(prog, "uTime");

    let raf = 0; const start = performance.now();
    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const w = Math.floor(canvas.clientWidth * dpr);
      const h = Math.floor(canvas.clientHeight * dpr);
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w; canvas.height = h;
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
    if (reduced) draw(0); else render();

    // A frozen shader still has to repaint on resize, or the canvas rescales to garbage.
    const onResize = () => (reduced ? draw(0) : resize());
    window.addEventListener("resize", onResize);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", onResize); };
  }, [reduced]);

  return (
    <canvas
      ref={ref}
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10 h-full w-full"
    />
  );
}
