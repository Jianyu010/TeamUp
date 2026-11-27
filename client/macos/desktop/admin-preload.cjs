const { contextBridge, ipcRenderer } = require("electron");

if (process.isMainFrame) {
  contextBridge.exposeInMainWorld("teamupAdmin", {
    signIn: () => ipcRenderer.invoke("teamup:admin-sign-in"),
  });
}
