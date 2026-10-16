import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // amazon-cognito-identity-js expects Node's `global` object
  define: { global: 'globalThis' },
});
