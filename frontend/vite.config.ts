import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// Built assets are served by the FastAPI app's `/ui` StaticFiles mount
// (api/app.py), never at the domain root — base must match that prefix or
// the built <script>/<link> URLs 404 in production.
export default defineConfig({
  base: '/ui/',
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // `/okf/*` is a sibling FastAPI router, not under `/ui` - proxy it
      // to the real API server so `npm run dev` works without CORS.
      '/okf': 'http://localhost:8000',
    },
  },
})
