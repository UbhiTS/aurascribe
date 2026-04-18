import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const SIDECAR_HTTP = 'http://127.0.0.1:8765'
const SIDECAR_WS = 'ws://127.0.0.1:8765'

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: '127.0.0.1',
    hmr: { protocol: 'ws', host: '127.0.0.1', port: 1421 },
    proxy: {
      '/api': SIDECAR_HTTP,
      '/ws': { target: SIDECAR_WS, ws: true },
    },
  },
  envPrefix: ['VITE_', 'TAURI_'],
})
