// THE CHROMATIC BLOOM — a waiting Spawn, no known end. Rendered per-card (own center, own seed) and
// screen-blended on the card's black plate, so only lit currents glow through and troughs add nothing.
// `noise.glsl` (precision + hash/noise/fbm) is prepended.
//
// The look is MASSES: each colour is one or more soft blooms that orbit the card's center, all turning
// the SAME way (a coherent swirl) at their own speeds. A mass blooms up from near-black and fades back
// on its own out-of-phase clock; its radius wanders freely (decoupled from the fade), so a card is
// sometimes mostly teal, later mostly violet, sometimes near-dark. Each mass is sampled THROUGH the
// domain-warp fbm rather than drawn as a clean gaussian, so its edges are inky and its path wanders —
// never a tidy circle. Space is aspect-corrected to a square first, so the swirl reads round on a wide
// short row instead of a flat pancake.

#define MASSES 6 // total blooms; colours cycle teal/cyan/violet, so this is 2 per colour.

uniform vec2 uRes;
uniform float uTime;
uniform float uSeed;                 // per-card: decorrelates neighbouring rows
uniform float uSpeed, uScale, uWarp, uRough, uGain;
uniform float uSpeedX, uSpeedY, uOrbitX, uOrbitY, uSquare;
uniform vec3 uTeal, uCyan, uViolet;

vec3 tintFor(float i) {
  float m = mod(i, 3.0);
  return m < 0.5 ? uTeal : (m < 1.5 ? uCyan : uViolet);
}

// Flatten a sinusoid toward +/-1: e=1 is the plain wave (round orbit), e->0 is a square wave (the point
// snaps to the extremes and dwells there — a rectangular path). Sign-preserving so both halves square off.
float sq(float v, float e) { return sign(v) * pow(abs(v), e); }

void main() {
  vec2 uv = gl_FragCoord.xy / uRes.xy;
  vec2 c = uv - 0.5;                 // centered, range +/-0.5 on BOTH axes (NOT aspect-corrected)
  // Left un-squared on purpose: squaring makes orbits round but confines colour to a central disc, so a
  // wide row's ends stay dark. In uv space a radius-~0.5 orbit reaches every edge, so the swirl fills the
  // card as a wide ellipse (full vertical travel too — not a flat pancake).
  float t = uTime * uSpeed;

  // Warp the sample point once, up front: gives every mass inky, flowing edges instead of hard gaussians.
  vec2 w = vec2(fbm(c * uScale + vec2(t * 0.2, uSeed), uRough),
                fbm(c * uScale + vec2(uSeed + 4.0, -t * 0.2), uRough));
  vec2 cp = c + uWarp * 0.15 * (w - 0.5);

  vec3 col = vec3(0.0);
  for (int i = 0; i < MASSES; i++) {
    float s = uSeed + float(i) * 17.3; // this mass's private seed

    // Coherent swirl at a per-mass rate. Each axis gets its OWN sinusoid so horizontal and vertical travel
    // can run at different speeds (uSpeedX/uSpeedY); the +90deg on y makes them circulate one way (an
    // orbit, not a back-and-forth bounce).
    float aSpeed = 0.25 + 0.55 * hash(vec2(s, 1.0));
    float base = 6.2831 * hash(vec2(s, 2.0));
    float ax = t * aSpeed * uSpeedX + base;
    float ay = t * aSpeed * uSpeedY + base + 1.5708;

    // Rectangular path: sq() flattens each wave toward +/-1 (uSquare 0 = round orbit, 1 = hard rectangle),
    // so the point hugs the card edges instead of tracing an oval. uOrbitX/Y are the extents (wide to fill
    // the row); rad breathes the whole path in and out over time, decoupled from the bloom clock.
    float rad = 0.5 + 0.55 * noise(vec2(t * 0.15 + s, s * 0.7));
    float e = mix(1.0, 0.12, uSquare);
    vec2 ctr = vec2(sq(sin(ax), e) * uOrbitX, sq(sin(ay), e) * uOrbitY) * rad;

    // Bloom envelope: swells from ~0 and fades back to 0 on this mass's own out-of-phase clock.
    float rate = 0.05 + 0.10 * hash(vec2(s, 3.0));
    float ph = fract(t * rate + hash(vec2(s, 4.0)));
    float env = smoothstep(0.0, 0.35, ph) * (1.0 - smoothstep(0.55, 1.0, ph));

    // Size drifts too. Falloff is measured on the WARPED point, which is what breaks the clean circle.
    // Big and soft so blooms overlap and reach into the corners rather than reading as separate dots.
    float size = 0.20 + 0.16 * noise(vec2(t * 0.2 + s, s + 9.0));
    float d = length(cp - ctr);
    float mass = env * exp(-(d * d) / (size * size));

    col += tintFor(float(i)) * mass;
  }

  col *= uGain;
  col += (hash(gl_FragCoord.xy) - 0.5) / 255.0; // dither
  gl_FragColor = vec4(max(col, 0.0), 1.0);
}
