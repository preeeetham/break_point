import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const api = process.env.FAULTLINE_API ?? "http://127.0.0.1:8777";

export default defineConfig({
  base: process.env.FABLE_BASE ?? "/fable/",
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5181,
    strictPort: true,
    proxy: { "/api": { target: api, changeOrigin: true } },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
