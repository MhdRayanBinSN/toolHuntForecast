import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { '/runs': 'http://127.0.0.1:8000', '/reports': 'http://127.0.0.1:8000', '/categories': 'http://127.0.0.1:8000', '/candidates': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000', '/screenshots': 'http://127.0.0.1:8000' } },
  build: { outDir: 'dist', emptyOutDir: true }
});
