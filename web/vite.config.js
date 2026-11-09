import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// In development the frontend runs on 5173 and proxies /api to the backend on 8000;
// in production the backend serves dist directly.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
