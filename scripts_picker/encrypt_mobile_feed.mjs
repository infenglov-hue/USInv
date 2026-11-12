import { createCipheriv, createHash, randomBytes } from "node:crypto";
import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const snapshotName = "mobile_snapshot.db.gz";
const encryptedName = `${snapshotName}.enc`;
const shellFiles = [
  "index.html",
  "index.css",
  "app.js",
  "sw.js",
  "portfolio-costs.js",
  "manifest.webmanifest",
  "icons/icon-192.png",
  "icons/icon-512.png",
  "vendor/pako.min.js",
  "vendor/sql-wasm.js",
  "vendor/sql-wasm.wasm",
  "vendor/apexcharts.min.js",
];

function parseMobileKey(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{43}$/.test(value)) {
    throw new Error("US_PICKER_MOBILE_KEY must be a 32-byte base64url key.");
  }
  const key = Buffer.from(value, "base64url");
  if (key.length !== 32 || key.toString("base64url") !== value) {
    throw new Error("US_PICKER_MOBILE_KEY must be a 32-byte base64url key.");
  }
  return key;
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

export async function assembleEncryptedMobileSite(sourceDir, siteDir, keyValue, pwaDir) {
  const key = parseMobileKey(keyValue);
  const manifest = await readFile(join(sourceDir, "manifest.json"));
  const snapshot = await readFile(join(sourceDir, snapshotName));
  let metadata;
  try {
    metadata = JSON.parse(manifest.toString("utf8"))?.snapshot;
  } catch {
    throw new Error("The private mobile feed manifest is invalid JSON.");
  }
  if (
    metadata?.filename !== snapshotName ||
    metadata.compression !== "gzip" ||
    metadata.size_bytes !== snapshot.length ||
    metadata.sha256 !== sha256(snapshot) ||
    snapshot[0] !== 0x1f || snapshot[1] !== 0x8b
  ) {
    throw new Error("The private SQLite snapshot does not match its manifest.");
  }

  const manifestLength = Buffer.alloc(4);
  manifestLength.writeUInt32BE(manifest.length);
  const nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  const encrypted = Buffer.concat([
    cipher.update(manifestLength),
    cipher.update(manifest),
    cipher.update(snapshot),
    cipher.final(),
    cipher.getAuthTag(),
  ]);

  // Requiring a fresh directory keeps private verification files out of the artifact.
  await mkdir(siteDir);
  for (const filename of shellFiles) {
    const target = join(siteDir, filename);
    await mkdir(dirname(target), { recursive: true });
    await copyFile(join(pwaDir, filename), target);
  }
  await writeFile(join(siteDir, encryptedName), encrypted);
  await writeFile(join(siteDir, "manifest.json"), JSON.stringify({
    encrypted_feed: {
      version: 1,
      filename: encryptedName,
      nonce: nonce.toString("base64url"),
      size_bytes: encrypted.length,
      sha256: sha256(encrypted),
    },
  }) + "\n");
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const [sourceDir, siteDir] = process.argv.slice(2);
  if (process.argv.length !== 4) {
    console.error("Usage: node scripts_picker/encrypt_mobile_feed.mjs SOURCE_DIR SITE_DIR");
    process.exitCode = 2;
  } else {
    try {
      await assembleEncryptedMobileSite(
        sourceDir, siteDir, process.env.US_PICKER_MOBILE_KEY, resolve(import.meta.dirname, "../pwa"),
      );
    } catch (error) {
      console.error(error.message);
      process.exitCode = 1;
    }
  }
}
