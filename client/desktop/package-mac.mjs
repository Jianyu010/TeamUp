import { packager } from "@electron/packager";
import { execFile } from "node:child_process";
import { copyFile, cp, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import checksums from "electron/checksums.json" with { type: "json" };
import manifest from "./package.json" with { type: "json" };

if (process.platform !== "darwin") {
  throw new Error("Build the macOS TeamUp app on a Mac.");
}

const desktop = import.meta.dirname;
const staging = await mkdtemp(join(tmpdir(), "teamup-desktop-"));
try {
  const source = join(staging, "app");
  await mkdir(source);
  await Promise.all([
    ...["main.cjs", "console-protocol.cjs", "launcher-command.cjs", "package.json"].map((name) =>
      copyFile(join(desktop, name), join(source, name))),
    cp(join(desktop, "../src/teamup_client/console"), join(source, "console"), {
      recursive: true,
    }),
    copyFile(join(desktop, "../../LICENSE"), join(source, "LICENSE")),
  ]);

  const icon = join(staging, "TeamUp.icns");
  await promisify(execFile)("/usr/bin/sips", [
    "-s", "format", "icns", join(desktop, "../../docs/assets/teamup-jet-pair.png"),
    "--out", icon,
  ]);
  const bundles = await packager({
    dir: source,
    out: join(desktop, "dist"),
    name: "TeamUp",
    platform: "darwin",
    arch: process.arch,
    electronVersion: manifest.devDependencies.electron,
    download: { checksums },
    appBundleId: "org.teamup.desktop",
    asar: true,
    extraResource: [join(desktop, "launch.py")],
    icon,
    osxSign: {
      identity: "-",
      identityValidation: false,
      continueOnError: false,
      preAutoEntitlements: false,
      // Local signatures have no Team ID for Electron's bundled frameworks.
      optionsForFile: () => ({ entitlements: join(desktop, "entitlements.mac.plist") }),
    },
    overwrite: true,
    prune: false,
  });
  for (const bundle of bundles) {
    console.log(join(bundle, "TeamUp.app"));
  }
} finally {
  await rm(staging, { recursive: true, force: true });
}
