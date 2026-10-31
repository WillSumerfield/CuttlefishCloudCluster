// Shared noise prelude. Concatenated AHEAD of every fragment shader (see gl.ts / the component that
// links it), so it must be self-contained and come first: it opens with the `precision` line the frag
// bodies rely on, then value noise and the domain-warp fbm both the ground and the bloom are built from.
precision highp float;

// fbm octave count. Compile-time constant (a GLSL ES 1.00 loop bound must be), so it lives here rather
// than as a uniform. Four is enough for broad masses without the cost of fine filaments.
#define OCTAVES 4

float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }

float noise(vec2 p) {
  vec2 i = floor(p); vec2 f = fract(p);
  float a = hash(i), b = hash(i + vec2(1.0, 0.0)), c = hash(i + vec2(0.0, 1.0)), d = hash(i + vec2(1.0, 1.0));
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// `rough` is the per-octave amplitude decay: LOW = broad masses, ~0.5 = classic fbm with fine wisps.
// Passed in (not baked) so each flow tunes its own detail against one shared implementation.
float fbm(vec2 p, float rough) {
  float v = 0.0; float amp = 0.5;
  for (int i = 0; i < OCTAVES; i++) { v += amp * noise(p); p *= 1.92; amp *= rough; }
  return v;
}
