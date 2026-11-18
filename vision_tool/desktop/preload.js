const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('visuvius', {
  platform: process.platform,
  setTextScale: pct => ipcRenderer.invoke('text-scale', pct),
  revert: () => ipcRenderer.invoke('revert'),
});
