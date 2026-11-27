import { packager } from "@electron/packager";
import { copyFile, cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, join, resolve, sep } from "node:path";
import checksums from "electron/checksums.json" with { type: "json" };
import manifest from "./package.json" with { type: "json" };

if (process.platform !== "win32") throw new Error("Build the Windows TeamUp app on Windows.");

const desktop = import.meta.dirname;
const output = resolve(desktop, "dist");
if (!output.startsWith(resolve(desktop) + sep)) throw new Error("Invalid package output directory.");
const staging = await mkdtemp(join(tmpdir(), "teamup-desktop-win-"));
try {
  const source = join(staging, "app");
  await mkdir(source);
  await Promise.all([
    ...["main.cjs", "console-protocol.cjs", "launcher-command.cjs", "package.json"].map((name) =>
      copyFile(join(desktop, name), join(source, name))),
    cp(join(desktop, "../../shared/src/teamup_client/console"), join(source, "console"), { recursive: true }),
    copyFile(join(desktop, "../../../LICENSE"), join(source, "LICENSE")),
  ]);

  // ICO supports a PNG payload; retain the existing 80x80 artwork byte-for-byte.
  const png = await readFile(join(source, "console/jet-pair.png"));
  const width = png.readUInt32BE(16), height = png.readUInt32BE(20);
  if (png.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a" ||
      width < 1 || width > 256 || height < 1 || height > 256) throw new Error("Invalid Windows icon.");
  const header = Buffer.alloc(22);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(1, 4);
  header[6] = width % 256;
  header[7] = height % 256;
  header.writeUInt16LE(1, 10);
  header.writeUInt16LE(32, 12);
  header.writeUInt32LE(png.length, 14);
  header.writeUInt32LE(22, 18);
  const icon = join(staging, "TeamUp.ico");
  await writeFile(icon, Buffer.concat([header, png]));

  const bundles = await packager({
    dir: source, out: output, name: "TeamUp", platform: "win32", arch: "x64",
    electronVersion: manifest.devDependencies.electron,
    download: { checksums },
    ...(process.env.TEAMUP_ELECTRON_ZIP_DIR ? { electronZipDir: resolve(process.env.TEAMUP_ELECTRON_ZIP_DIR) } : {}),
    asar: true, icon,
    extraResource: [join(desktop, "launch.py"), join(desktop, "launch-wsl.py")],
    win32metadata: { CompanyName: "TeamUp", FileDescription: "TeamUp desktop console", ProductName: "TeamUp" },
    overwrite: true, prune: false,
  });
  for (const bundle of bundles) {
    await copyFile(join(desktop, "install-win.ps1"), join(bundle, "Install-TeamUp.ps1"));
    console.log(join(bundle, "TeamUp.exe"));
  }
} finally {
  const temporaryRoot = resolve(tmpdir()) + sep;
  if (!resolve(staging).startsWith(temporaryRoot) || !basename(staging).startsWith("teamup-desktop-win-")) {
    throw new Error("Refusing to remove an unexpected staging directory.");
  }
  await rm(staging, { recursive: true, force: true });
}
