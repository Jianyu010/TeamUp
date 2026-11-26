const assert = require("node:assert/strict");
const { createHash, randomBytes } = require("node:crypto");
const { mkdtemp, readFile, readdir, rm, writeFile } = require("node:fs/promises");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const test = require("node:test");
const { createKeychain } = require("../../../../client/macos/desktop/keychain.cjs");

function fakeStorage() {
  const encrypted = new Map();
  return {
    isAsyncEncryptionAvailable: async () => true,
    async encryptStringAsync(plaintext) {
      const token = randomBytes(32);
      encrypted.set(token.toString("hex"), plaintext);
      return token;
    },
    async decryptStringAsync(token) {
      const plaintext = encrypted.get(token.toString("hex"));
      if (plaintext === undefined) throw new Error("Test codec could not decrypt the record");
      return { result: plaintext, shouldReEncrypt: false };
    },
  };
}

async function fakeKeychain(t, safeStorage = fakeStorage()) {
  const directory = await mkdtemp(join(tmpdir(), "teamup-test-keychain-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const helper = join(directory, "helper");
  await writeFile(helper, `#!/usr/bin/env node
const { readFileSync, writeFileSync, rmSync } = require("node:fs");
const { join } = require("node:path");
const [operation, account, ...extra] = process.argv.slice(2);
if (!/^[0-9a-f]{64}$/.test(account) || extra.length) process.exit(1);
const item = join(__dirname, account);
try {
  if (operation === "get") process.stdout.write(readFileSync(item));
  else if (operation === "put") writeFileSync(item, readFileSync(0));
  else if (operation === "forget") rmSync(item, { force: true });
  else process.exit(1);
} catch (error) {
  process.exit(error.code === "ENOENT" ? 2 : 1);
}
`, { mode: 0o700 });
  return { directory, helper, keychain: createKeychain(helper, safeStorage) };
}

test("Keychain helper gets only ciphertext scoped to the profile", async (t) => {
  const { directory, keychain } = await fakeKeychain(t);
  const profile = join(directory, "profile with spaces.json");
  const other = join(directory, "another.json");
  const credentials = {
    username: "TeamUp test",
    password: "synthetic\n'🔑'\tpayload",
    scope: "synthetic-server-scope",
  };

  assert.equal(await keychain.get(profile), null);
  await keychain.put(profile, credentials);
  const account = createHash("sha256").update(profile).digest("hex");
  const helperRecord = await readFile(join(directory, account));
  for (const field of Object.values(credentials)) {
    assert.equal(helperRecord.includes(field), false);
  }
  assert.deepEqual(await keychain.get(profile), credentials);
  assert.equal(await keychain.get(other), null);
  const otherAccount = createHash("sha256").update(other).digest("hex");
  await writeFile(join(directory, otherAccount), helperRecord);
  await assert.rejects(keychain.get(other), {message: "Could not access the macOS Keychain."});

  const updated = { ...credentials, password: "new synthetic value" };
  await keychain.put(profile, updated);
  assert.deepEqual(await keychain.get(profile), updated);

  await keychain.forget(profile);
  await keychain.forget(profile);
  assert.equal(await keychain.get(profile), null);
});

test("Keychain errors hide helper output and reject invalid or oversized records", async (t) => {
  const { directory, helper, keychain } = await fakeKeychain(t);
  const profile = join(directory, "profile.json");
  const genericError = { message: "Could not access the macOS Keychain." };
  for (const program of [
    'process.stderr.write("synthetic secret"); process.exitCode = 1;',
    'process.stdout.write("invalid synthetic record");',
    'process.stdout.write(JSON.stringify({username: "sample", password: null}));',
    'process.stdout.write("x".repeat(1024 * 16));',
  ]) {
    await writeFile(helper, `#!/usr/bin/env node\n${program}\n`);
    await assert.rejects(keychain.get(profile), genericError);
  }
});

test("Keychain refuses to store credentials when encryption is unavailable", async (t) => {
  const storage = fakeStorage();
  storage.isAsyncEncryptionAvailable = async () => false;
  const { directory, keychain } = await fakeKeychain(t, storage);
  await assert.rejects(
    keychain.put(join(directory, "profile.json"), {
      username: "test account",
      password: "synthetic test password",
      scope: "synthetic-server-scope",
    }),
    { message: "Could not access the macOS Keychain." },
  );
  assert.deepEqual(await readdir(directory), ["helper"]);
});

test("Keychain re-encrypts a credential after the OS rotates its key", async (t) => {
  const storage = fakeStorage();
  const unwrap = storage.decryptStringAsync;
  storage.decryptStringAsync = async (encrypted) => ({
    ...await unwrap(encrypted),
    shouldReEncrypt: true,
  });
  const { directory, keychain } = await fakeKeychain(t, storage);
  const profile = join(directory, "profile.json");
  const item = join(directory, createHash("sha256").update(profile).digest("hex"));
  const credentials = {
    username: "test account",
    password: "synthetic test password",
    scope: "synthetic-server-scope",
  };
  await keychain.put(profile, credentials);
  const previous = await readFile(item);
  assert.deepEqual(await keychain.get(profile), credentials);
  assert.notDeepEqual(await readFile(item), previous);
});
