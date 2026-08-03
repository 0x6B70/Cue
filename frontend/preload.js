/**
 * Preload for the search window.
 *
 * The UI talks to the backend over plain HTTP on the same origin it was
 * served from, so it needs almost nothing from Electron. This exposes only a
 * flag the page uses to tweak chrome-specific behaviour.
 */

const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('desktop', {
  isElectron: true,
  platform: process.platform
});
