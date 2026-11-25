const loadTimeoutMs = 12000;

function block(event) {
  event.preventDefault();
}

function createRemoteWindow({ BrowserWindow, dialog, remoteSession, title, resolveView, retryHint }) {
  let window;
  let opening;
  remoteSession.setPermissionCheckHandler(() => false);
  remoteSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  remoteSession.on("will-download", block);

  async function open() {
    const { landing, allowsRequest, allowsNavigation, requiredAssets } = await resolveView();
    remoteSession.webRequest.onBeforeRequest((request, callback) => {
      callback({ cancel: !allowsRequest(request) });
    });
    const candidate = new BrowserWindow({
      title,
      width: 1000,
      height: 800,
      minWidth: 680,
      minHeight: 520,
      show: false,
      webPreferences: {
        session: remoteSession,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    window = candidate;
    candidate.once("closed", () => { window = null; });
    candidate.webContents.on("will-frame-navigate", (event) => {
      if (!event.isMainFrame || !allowsNavigation?.(event.url)) block(event);
    });
    candidate.webContents.on("will-redirect", block);
    candidate.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

    let status;
    candidate.webContents.once("did-navigate", (_event, _url, code) => { status = code; });
    const pendingAssets = new Set(requiredAssets.map((path) => new URL(path, landing).href));
    remoteSession.webRequest.onHeadersReceived((response, callback) => {
      if (response.statusCode === 200 || response.statusCode === 304) {
        pendingAssets.delete(response.url);
      }
      callback({});
    });

    let timeout;
    const deadline = new Promise((_resolve, reject) => {
      timeout = setTimeout(() => reject(new Error(`Opening ${title} timed out.`)), loadTimeoutMs);
    });
    try {
      await Promise.race([candidate.loadURL(landing.href), deadline]);
      if (status !== 200) {
        throw new Error(`The ${title} page at ${landing.pathname} did not load.`);
      }
      if (pendingAssets.size) {
        throw new Error(`The ${title} page did not load all required assets.`);
      }
      candidate.show();
    } catch (error) {
      if (candidate.isDestroyed()) return;
      candidate.destroy();
      throw error;
    } finally {
      clearTimeout(timeout);
      remoteSession.webRequest.onHeadersReceived(null);
    }
  }

  return function showRemoteWindow() {
    if (opening) return;
    if (window) {
      if (window.isMinimized()) window.restore();
      window.show();
      window.focus();
      return;
    }
    opening = open().catch((error) => {
      dialog.showErrorBox(`Could not open ${title}`, `${error.message}\n\n${retryHint}`);
    }).finally(() => { opening = null; });
  };
}

module.exports = { createRemoteWindow };
