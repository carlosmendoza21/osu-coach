import { defineConfig } from 'vitest/config'

// The live tests (npm run test:live): they call the real API, so they need it running
// (docker compose up -d). `npm test` leaves them out.
export default defineConfig({
  test: {
    include: ['live/**/*.live.test.ts'],
    environment: 'node',
    testTimeout: 90_000, // a first player sync goes through the worker and osu!
    hookTimeout: 30_000,
  },
})
