import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The dashboard service serves the built SPA and the API on ONE origin, so `/arms` and `/spawns` are
// same-origin in production. In `npm run dev`, proxy them to the local dashboard service (:8082).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/arms': 'http://localhost:8082',
      '/spawns': 'http://localhost:8082',
    },
  },
})
