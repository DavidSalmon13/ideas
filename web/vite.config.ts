/// <reference types="vitest" />
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// The SPA is a client of the API (plan.md §12). In dev it talks to the engine at
// VITE_API_BASE (default http://127.0.0.1:8000; CORS is open). Built and served by the
// API itself, VITE_API_BASE is empty and calls are same-origin.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
    css: false,
  },
})
