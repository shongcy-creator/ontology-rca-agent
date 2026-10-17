import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8088',
        changeOrigin: true,
        rewrite: (path) => path,  // keep /api prefix
      },
    },
  },
  build: {
    outDir: 'dist',
    // 生产构建不生成 sourcemap：dist 会被 Dockerfile 整体拷进 nginx 镜像，
    // .map 文件会暴露源码路径与映射（SECURITY 口径：不泄露实现细节）。
    // 本地排障需要时可用 `vite build --sourcemap` 单独开。
    sourcemap: false,
  },
})
