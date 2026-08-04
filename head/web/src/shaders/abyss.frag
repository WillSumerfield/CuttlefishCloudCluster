// THE GROUND — slow deep water, blue-green ink. Domain-warped fbm, viewed directly against black (not
// screen-blended), so it may stay dark. `noise.glsl` (precision + hash/noise/fbm) is prepended.

uniform vec2 uRes;
uniform float uTime;
uniform float uSpeed, uScale, uWarp, uRough;
uniform float uHueFrom, uHueTo, uWaterFrom, uWaterTo, uVignette;
uniform vec3 uInk, uGreen, uBlue, uLift;

void main() {
  vec2 uv = gl_FragCoord.xy / uRes.xy;
  vec2 p = uv * vec2(uRes.x / uRes.y, 1.0) * uScale;
  float t = uTime * uSpeed;

  // domain warp — this is what turns noise into currents
  vec2 q = vec2(fbm(p + vec2(t * 0.6, t * 0.8), uRough), fbm(p + vec2(5.2 - t * 0.5, -t * 0.7), uRough));
  vec2 r = vec2(fbm(p + uWarp * q + vec2(1.7 + t * 1.1, 9.2 - t * 0.4), uRough),
                fbm(p + uWarp * q + vec2(8.3 - t * 0.9, 2.8 + t * 1.2), uRough));
  float f = fbm(p + uWarp * 0.9 * r + vec2(t * 0.3, -t * 0.2), uRough);

  // A second, slower field decides the HUE, so green and blue currents drift through each other
  // independently of where the light/dark bands fall.
  float hue = fbm(p * 0.45 + vec2(-t * 0.22, t * 0.17), uRough);
  hue = smoothstep(uHueFrom, uHueTo, hue);

  vec3 current = mix(uGreen, uBlue, hue);
  vec3 col = mix(uInk, current, smoothstep(uWaterFrom, uWaterTo, f));
  col = mix(col, uLift, smoothstep(0.52, 1.05, f * f));

  // very faint warm undertone in the deepest currents — never red
  col += 0.010 * vec3(0.7, 0.55, 0.30) * smoothstep(0.72, 1.0, r.x);

  // slow vignette so surfaces read cleaner
  float d = distance(uv, vec2(0.5, 0.42));
  col *= 1.0 - smoothstep(0.35, 1.05, d) * uVignette;

  col += (hash(gl_FragCoord.xy) - 0.5) / 255.0; // dither kills banding
  gl_FragColor = vec4(col, 1.0);
}
