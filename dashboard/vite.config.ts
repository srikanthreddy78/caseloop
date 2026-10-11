import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/dashboard/",
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://localhost:8000",
      "/dev": "http://localhost:8000",
      "/widget.js": "http://localhost:8000",
    },
  },
});
