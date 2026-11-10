import react from '@vitejs/plugin-react'
import { configDefaults, defineConfig } from 'vitest/config'

// The backend (FastAPI) runs on port 8000. In development, Vite forwards /api/... to it
// without the /api prefix, so the browser only ever talks to one site (no CORS).
const API = process.env.API_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: API,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom', // a browser-like page for components, without a browser
    setupFiles: ['./src/test/setup.ts'],
    // The live tests need the API running: npm run test:live (vitest.live.config.ts).
    exclude: [...configDefaults.exclude, 'live/**'],
  },
})
