import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    // Just above elkjs, which is one GWT-compiled vendor file at ~1.4 MB and
    // has no smaller build. It is loaded on demand (`layout/elkLayout.ts`), so
    // it is not in the initial payload — but rollup measures chunks, not
    // payloads, and a warning that fires on every single build is one nobody
    // reads. Set here so that a *new* chunk crossing this line is a real
    // finding rather than the fourth line of expected noise.
    //
    // The number that actually matters is what the browser needs before it can
    // paint: 293 kB, 94 kB gzipped, plus 22 kB of CSS.
    chunkSizeWarningLimit: 1500,
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    // The backend binds to loopback by design; the dev server proxies so the
    // browser talks to one origin and CORS stays out of the picture.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true, ws: true },
    },
  },
});
