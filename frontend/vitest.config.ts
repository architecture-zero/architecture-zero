import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/__tests__/setup.ts'],
    // CI runners are shared and can be loaded: a file that takes 2 s on a
    // workstation took 17 s on a busy runner (2026-10-08), and a test that
    // takes 50 ms there crossed vitest's 5 s default (adminWriteSafety, and
    // historyList before it). 20 s still fails a test that hangs.
    testTimeout: 20000,
  },
})
