const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('visuvius', {
  platform: process.platform,
  apply: opts => ipcRenderer.invoke('apply', opts),
  revert: () => ipcRenderer.invoke('revert'),
});
