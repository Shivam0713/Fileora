import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
// Use IPv4 explicitly so Vite reaches the Docker-published API on Windows,
// where `localhost` may resolve to IPv6 before the backend listener is ready.
export default defineConfig({ plugins: [react(), tailwindcss()], server: { proxy: { '/api': 'http://127.0.0.1:8000' } } });
