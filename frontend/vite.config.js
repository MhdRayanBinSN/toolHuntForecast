import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000';
const apiPaths = ['/runs', '/reports', '/categories', '/candidates', '/health', '/screenshots'];

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: Object.fromEntries(apiPaths.map(path => [path, { target: apiTarget }])) },
  build: { outDir: 'dist', emptyOutDir: true }
});
