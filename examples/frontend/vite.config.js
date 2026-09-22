import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// astra REST API base URL — override with VITE_ASTRA_API_URL in a .env
// file if the backend isn't on the default http://localhost:8000
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
});
