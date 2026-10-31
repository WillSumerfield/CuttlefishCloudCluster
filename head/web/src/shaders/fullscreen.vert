// Fullscreen triangle-pair. The only vertex shader either flow uses; positions come straight from the
// clip-space quad bound to `a`, so the fragment shader runs once per pixel with nothing to interpolate.
attribute vec2 a;
void main() { gl_Position = vec4(a, 0.0, 1.0); }
