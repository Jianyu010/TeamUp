const { execFile } = require("node:child_process");
const { createHash } = require("node:crypto");
const { resolve } = require("node:path");

const recordLimit = 8192;
const unavailable = () => new Error("Could not access the macOS Keychain.");

function credentialRecord(value) {
  if (!value || !["username", "password", "scope"].every(
    (key) => typeof value[key] === "string" && value[key].length > 0
  )) {
    throw unavailable();
  }
  const { username, password, scope } = value;
  return { username, password, scope };
}

function accountFor(profilePath) {
  return createHash("sha256").update(resolve(profilePath)).digest("hex");
}

function createKeychain(executable, safeStorage) {
  function run(operation, account, input) {
    return new Promise((resolveResult, reject) => {
      const child = execFile(executable, [operation, account], {
        encoding: "buffer",
        maxBuffer: recordLimit,
        timeout: 5000,
        killSignal: "SIGKILL",
      }, (error, stdout) => {
        if (operation === "get" && error?.code === 2) resolveResult(null);
        else if (error) reject(unavailable());
        else resolveResult(stdout);
      });
      // A locked Keychain can cause the helper to exit before reading stdin.
      child.stdin.on("error", () => {});
      child.stdin.end(input);
    });
  }

  async function withEncryption(operation) {
    try {
      if (await safeStorage.isAsyncEncryptionAvailable()) return await operation();
    } catch {
      // Neither the renderer nor logs receive errors from the encryption backend.
    }
    throw unavailable();
  }

  async function put(profilePath, credentials) {
    const account = accountFor(profilePath);
    const encrypted = await withEncryption(() => safeStorage.encryptStringAsync(
      JSON.stringify({ account, ...credentialRecord(credentials) })
    ));
    if (!Buffer.isBuffer(encrypted) || !encrypted.length || encrypted.length > recordLimit) {
      throw unavailable();
    }
    await run("put", account, encrypted);
  }

  return {
    async get(profilePath) {
      const account = accountFor(profilePath);
      const encrypted = await run("get", account);
      if (encrypted === null) return null;
      const { credentials, shouldReEncrypt } = await withEncryption(async () => {
        const { result, shouldReEncrypt } = await safeStorage.decryptStringAsync(encrypted);
        const record = JSON.parse(result);
        if (record?.account !== account) throw unavailable();
        return { credentials: credentialRecord(record), shouldReEncrypt };
      });
      if (shouldReEncrypt) await put(profilePath, credentials);
      return credentials;
    },
    put,
    async forget(profilePath) {
      await run("forget", accountFor(profilePath));
    },
  };
}

module.exports = { createKeychain };
