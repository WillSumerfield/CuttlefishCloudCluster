// Tiny WebGL plumbing shared by every flow. No state of its own — just compile/link and the fullscreen
// quad bind. Dials live in each component's JS object and are pushed as uniforms; these helpers only get
// a program running. A failed compile/link throws (a dead shader is otherwise invisible on a black page).

export function compile(gl: WebGLRenderingContext, type: number, src: string): WebGLShader {
  const s = gl.createShader(type)!;
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || "shader compile failed");
  return s;
}

export function program(gl: WebGLRenderingContext, vsrc: string, fsrc: string): WebGLProgram {
  const p = gl.createProgram()!;
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vsrc));
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fsrc));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || "program link failed");
  gl.useProgram(p);
  return p;
}

// The standard two-triangle fullscreen quad, bound to attribute `a`. Drawn with drawArrays(TRIANGLES,0,6).
export function fullscreenQuad(gl: WebGLRenderingContext, prog: WebGLProgram): void {
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
  const a = gl.getAttribLocation(prog, "a");
  gl.enableVertexAttribArray(a);
  gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
}
