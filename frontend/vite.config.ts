import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    allowedHosts: true,
    watch: {
      // Bind mounts on Docker Desktop do not deliver inotify events.
      usePolling: true,
    },
    proxy: {
      // Same-origin in dev, matching what nginx.conf does in the built image:
      // VITE_API_URL stays empty, the browser sees one origin, and the
      // backend's server-side origin check is satisfied without CORS.
      // `backend` is the compose service name; running the backend on the host
      // instead means pointing this at http://localhost:8000.
      '/api': {
        target: 'http://backend:8000',
        changeOrigin: true,
      },
    },
  },
})
