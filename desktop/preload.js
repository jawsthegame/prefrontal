// Exposes a tiny, explicit bridge to the renderer (used by settings.html). The
// dashboard itself is served remotely and ignores this — it only reaches these
// APIs from our own local pages, which is why contextIsolation stays on.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('prefrontal', {
  getServerUrl: () => ipcRenderer.invoke('get-server-url'),
  setServerUrl: (url) => ipcRenderer.invoke('set-server-url', url),
});
