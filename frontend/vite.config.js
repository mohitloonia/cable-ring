import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend address for /api calls. Default 8000; set API_PORT to use another port.
const apiPort = process.env.API_PORT || "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": `http://127.0.0.1:${apiPort}`,
    },
  },
});
