import { FuseV1Options, FuseVersion, flipFuses } from "@electron/fuses";
import { packager } from "@electron/packager";
import { execFile } from "node:child_process";
import { copyFile, cp, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { promisify } from "node:util";
import checksums from "electron/checksums.json" with { type: "json" };
import manifest from "./package.json" with { type: "json" };

if (process.platform !== "darwin") {
  throw new Error("Build the macOS TeamUp app on a Mac.");
}

const desktop = import.meta.dirname;
const exec = promisify(execFile);
const { stdout } = await exec("python3", [
  "-c", "from scripts.package_version import __version__; print(__version__)",
], { cwd: join(desktop, "../../..") });
const teamupVersion = stdout.trim();
// macOS bundle metadata requires a numeric version. The About panel uses the full build string.
const appVersion = teamupVersion.replace(/(?:\.dev[0-9]+|\+unknown)$/, "");

const staging = await mkdtemp(join(tmpdir(), "teamup-desktop-"));
try {
  const source = join(staging, "app");
  const keychain = join(staging, "teamup-keychain");
  await mkdir(source);
  await Promise.all([
    ...["main.cjs", "console-protocol.cjs", "console-login.cjs", "keychain.cjs"].map((name) =>
      copyFile(join(desktop, name), join(source, name))),
    writeFile(join(source, "package.json"), JSON.stringify({...manifest, teamupVersion})),
    cp(join(desktop, "../../shared/src/teamup_client/console"), join(source, "console"), {
      recursive: true,
    }),
    copyFile(join(desktop, "../../../LICENSE"), join(source, "LICENSE")),
    exec(process.execPath, [join(desktop, "build-keychain.mjs"), keychain]),
  ]);

  const icon = join(staging, "TeamUp.icns");
  await exec("/usr/bin/sips", [
    "-s", "format", "icns", join(desktop, "../../../docs/assets/teamup-jet-pair.png"),
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
    appVersion,
    appBundleId: "org.teamup.desktop",
    asar: true,
    // Only the signed app's packaged code can use its Keychain identity.
    afterExtract: [({ buildPath }) => flipFuses(join(buildPath, "Electron.app"), {
      version: FuseVersion.V1,
      [FuseV1Options.RunAsNode]: false,
      [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
      [FuseV1Options.EnableNodeCliInspectArguments]: false,
      [FuseV1Options.EnableEmbeddedAsarIntegrityValidation]: true,
      [FuseV1Options.OnlyLoadAppFromAsar]: true,
    })],
    extraResource: [join(desktop, "launch.py"), keychain],
    icon,
    osxSign: {
      identity: "-",
      identityValidation: false,
      continueOnError: false,
      preAutoEntitlements: false,
      // Local signatures have no Team ID for Electron's bundled frameworks.
      optionsForFile: (path) => ({
        entitlements: basename(path) === "teamup-keychain"
          ? [] : join(desktop, "entitlements.mac.plist"),
      }),
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
