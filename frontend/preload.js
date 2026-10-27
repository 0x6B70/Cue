/**
 * Preload for the search window.
 *
 * The UI talks to the backend over plain HTTP on the same origin it was
 * served from, so it needs almost nothing from Electron. This exposes a flag
 * the page uses to tweak chrome-specific behaviour, and the native folder
 * picker, which no web page can open on its own.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktop', {
  isElectron: true,
  platform: process.platform,
  chooseDirectory: (current) => ipcRenderer.invoke('choose-directory', current)
});
