import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// 构建产物直接落 FastAPI 的静态目录：运行时零构建、零外网依赖。
export default defineConfig({
  plugins: [react()],
  base: '/static/spa/',
  build: {
    outDir: path.resolve(__dirname, '../app/admin/static/spa'),
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:18003',
    },
  },
});
