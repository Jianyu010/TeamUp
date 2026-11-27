import { execFile } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { promisify } from "node:util";

const desktop = import.meta.dirname;
const output = process.argv[2] ?? join(desktop, "dist", "teamup-keychain");
await mkdir(dirname(output), { recursive: true });
await promisify(execFile)("/usr/bin/xcrun", [
  "clang", "-arch", process.arch === "arm64" ? "arm64" : "x86_64",
  "-mmacosx-version-min=13.0", "-fobjc-arc", "-O2", "-Wall", "-Wextra", "-Werror",
  "-framework", "Foundation", "-framework", "Security",
  join(desktop, "teamup-keychain.m"), "-o", output,
]);
