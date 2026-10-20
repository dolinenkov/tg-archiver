import { defineConfig } from 'vite';

export default defineConfig({
  publicDir: 'frontend/public',
  build: {
    outDir: 'tg_archiver/static',
    emptyOutDir: true,
    modulePreload: false,
    rolldownOptions: {
      input: 'frontend/main.ts',
      output: { entryFileNames: 'app.js', assetFileNames: 'app[extname]' },
    },
  },
});
