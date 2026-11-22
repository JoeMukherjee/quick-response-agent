import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The build is committed into the Python package, so `python -m qra serve` needs no Node.
export default defineConfig({
  plugins: [react()],
  base: '/static/',
  build: { outDir: '../qra/server/static', emptyOutDir: true, sourcemap: false, chunkSizeWarningLimit: 600 },
  server: { proxy: { '/api': 'http://127.0.0.1:8765' } },
});
