const {requestPrivateLogin} = require("./console-protocol.cjs");

function createDesktopLogin(keychain, profile) {
  let pending = Promise.resolve();
  function run(operation) {
    const attempt = pending.then(operation);
    pending = attempt.catch(() => {});
    return attempt;
  }

  return {
    async status(endpoint, signal) {
      const {status, result} = await requestPrivateLogin(endpoint, undefined, signal);
      return {
        status,
        result: {state: result.state, remember_available: typeof result.remember_scope === "string"},
      };
    },

    restore(endpoint) {
      return run(async () => {
        if (!endpoint.loginSocket) return;
        try {
          const {result: state} = await requestPrivateLogin(endpoint);
          if (state.state !== "sign_in_required" || typeof state.remember_scope !== "string") return;
          const saved = await keychain.get(profile);
          if (saved?.scope !== state.remember_scope) return;
          const {status, result} = await requestPrivateLogin(endpoint, {
            username: saved.username,
            password: saved.password,
            remember_scope: saved.scope,
          });
          if (status === 401 && result.state === "wrong_credentials") {
            await keychain.forget(profile);
          }
        } catch {
          // The interactive form remains available if the saved sign-in cannot run.
        }
      });
    },

    signIn(endpoint, {username, password, remember = false}, signal) {
      return run(async () => {
        const {status, result} = await requestPrivateLogin(endpoint, {username, password}, signal);
        const {state, authenticated_scope} = result;
        const view = {state};
        if (state === "ready" && typeof authenticated_scope === "string") {
          try {
            if (remember) {
              await keychain.put(profile, {username, password, scope: authenticated_scope});
            } else {
              await keychain.forget(profile);
            }
          } catch {
            view.keychain = remember ? "save_failed" : "forget_failed";
          }
        } else if (state === "ready" && remember) {
          view.keychain = "save_failed";
        }
        return {status, result: view};
      });
    },

    forget() {
      return run(() => keychain.forget(profile));
    },
  };
}

module.exports = {createDesktopLogin};
