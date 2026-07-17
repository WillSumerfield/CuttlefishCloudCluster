import { existsSync, readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Serve dev over HTTPS when the Head's self-signed cert is present. The session cookie is `Secure`
// (the Head is HTTPS in production), and a browser won't store a Secure cookie delivered to an
// http:// origin — so over plain-http dev every refresh lost the session and bounced to sign-in.
// Reusing the Head cert makes the dev origin https://localhost:5173, matching production exactly.
// Absent (a fresh checkout with no Head box), fall back to HTTP — login still works there via the
// prod origin, and this keeps the config from crashing.
const CERT = '/opt/ccc/tls/head.crt'
const KEY = '/opt/ccc/tls/head.key'
const https = existsSync(CERT) && existsSync(KEY)
  ? { cert: readFileSync(CERT), key: readFileSync(KEY) }
  : undefined

// The dashboard service serves the built SPA and the API on ONE origin, so every API call is
// same-origin in production. In `npm run dev` the SPA is on :5173, so proxy the API to the local
// dashboard service (:8082). Every endpoint lives under `/api` (see api.ts), so one prefix covers the
// whole API — and, crucially, `/api/*` can never shadow a client route like `/karma`, so a hard reload
// there falls through to Vite's SPA index.html instead of proxying to the Head and rendering JSON.
//
// The service speaks HTTPS with the cert the Head signs itself (see ccc_head_api.main docstring), so
// the target is https and `secure:false` — Vite must not reject the self-signed cert. Without this the
// proxy can't connect and every API call comes back to the browser as a 500.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    https,
    proxy: {
      '/api': { target: 'https://localhost:8082', changeOrigin: true, secure: false },
    },
  },
})
