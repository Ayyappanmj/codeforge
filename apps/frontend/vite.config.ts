import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, proxy /api to the API container/host so the browser stays same-origin.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://localhost:8000" } },
});
