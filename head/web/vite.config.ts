import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Serve dev over HTTPS when the Head's self-signed cert is present. The session cookie is `Secure`
// (the Head is HTTPS in production), and a browser won't store a Secure cookie delivered to an
// http:// origin — so over plain-http dev every refresh lost the session and bounced to sign-in.
// Reusing the Head cert makes the dev origin https://localhost:5173, matching production exactly.
const CERT = '/opt/ccc/tls/head.crt'
const KEY = '/opt/ccc/tls/head.key'

/**
 * The keypair, or undefined — and it must be ABLE to fail, not merely allowed to be absent.
 *
 * This used to be `existsSync(CERT) && existsSync(KEY) ? readFileSync(...) : undefined`, which reads as
 * a guard and is not one: `head.key` is 0600 `ccc:ccc` (setup._secure_tls tightened it there on
 * 2026-08-01, correctly — a Guise could otherwise read the private half of the cert its own Head
 * authenticates with). So on a real Head the file plainly exists and the admin plainly cannot read it,
 * the exists-check passes, and readFileSync throws EACCES while vite is still loading its config.
 *
 * That took `setup --head` down with it, because build_spa() runs `npm run build` as the invoking admin
 * — so the Dashboard could not be rebuilt on the one machine that serves it. Unreadable is a normal
 * state here, not an error: fall back to HTTP, exactly as an absent cert already did.
 */
function devHttps() {
  try {
    return { cert: readFileSync(CERT), key: readFileSync(KEY) }
  } catch {
    return undefined // absent (fresh checkout), or unreadable (not the `ccc` user) — both mean HTTP
  }
}

// The dashboard service serves the built SPA and the API on ONE origin, so every API call is
// same-origin in production. In `npm run dev` the SPA is on :5173, so proxy the API to the local
// dashboard service (:443). Every endpoint lives under `/api` (see api.ts), so one prefix covers the
// whole API — and, crucially, `/api/*` can never shadow a client route like `/karma`, so a hard reload
// there falls through to Vite's SPA index.html instead of proxying to the Head and rendering JSON.
//
// The service speaks HTTPS with the cert the Head signs itself (see ccc_head_api.main docstring), so
// the target is https and `secure:false` — Vite must not reject the self-signed cert. Without this the
// proxy can't connect and every API call comes back to the browser as a 500.
//
// Config is a FUNCTION of the command so the keypair is only ever touched by `serve`. `vite build`
// wants no dev server and no TLS, and a build has no business reading the Head's private key to
// produce static files — the try/catch above is the safety net, this is the reason it rarely fires.
export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  server: {
    https: command === 'serve' ? devHttps() : undefined,
    proxy: {
      '/api': { target: 'https://localhost', changeOrigin: true, secure: false },
    },
  },
}))
