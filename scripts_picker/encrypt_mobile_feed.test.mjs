import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { gzipSync } from "node:zlib";
import { assembleEncryptedMobileSite } from "./encrypt_mobile_feed.mjs";

const fixtureKey = Buffer.alloc(32, 1);
const pwaDir = resolve(import.meta.dirname, "../pwa");

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), "us-picker-encrypted-test-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const source = join(directory, "private-feed");
  const target = join(directory, "site");
  await mkdir(source);
  const snapshot = gzipSync("Synthetic fixture, no market prices");
  const manifest = Buffer.from(JSON.stringify({
    snapshot: {
      filename: "mobile_snapshot.db.gz",
      compression: "gzip",
      size_bytes: snapshot.length,
      sha256: sha256(snapshot),
    },
    rotation: { date: "2026-01-05" },
  }));
  await writeFile(join(source, "manifest.json"), manifest);
  await writeFile(join(source, "mobile_snapshot.db.gz"), snapshot);
  await writeFile(join(source, "live_tickers.json"), "{\"tickers\":[\"FIXTURE\"]}");
  return { source, target, manifest, snapshot };
}

test("the Pages artifact contains only shell files and an authenticated encrypted feed", async (t) => {
  const { source, target, manifest, snapshot } = await fixture(t);
  await assembleEncryptedMobileSite(source, target, fixtureKey.toString("base64url"), pwaDir);
  const files = await readdir(target);
  assert.deepEqual(files.sort(), [
    "app.js", "icons", "index.css", "index.html", "manifest.json", "manifest.webmanifest",
    "mobile_snapshot.db.gz.enc", "portfolio-costs.js", "sw.js", "vendor",
  ].sort());

  const published = JSON.parse(await readFile(join(target, "manifest.json"), "utf8"));
  assert.deepEqual(Object.keys(published), ["encrypted_feed"]);
  const envelope = published.encrypted_feed;
  assert.equal(envelope.version, 1);
  assert.equal(envelope.filename, "mobile_snapshot.db.gz.enc");
  const encrypted = await readFile(join(target, envelope.filename));
  assert.equal(envelope.size_bytes, encrypted.length);
  assert.equal(envelope.sha256, sha256(encrypted));
  const nonce = Buffer.from(envelope.nonce, "base64url");
  assert.equal(nonce.length, 12);
  const key = await webcrypto.subtle.importKey("raw", fixtureKey, "AES-GCM", false, ["decrypt"]);
  const algorithm = { name: "AES-GCM", iv: nonce };
  const plaintext = Buffer.from(await webcrypto.subtle.decrypt(algorithm, key, encrypted));
  const length = plaintext.readUInt32BE();
  assert.equal(length, manifest.length);
  assert.deepEqual(plaintext.subarray(4, 4 + length), manifest);
  assert.deepEqual(plaintext.subarray(4 + length), snapshot);

  encrypted[0] ^= 1;
  await assert.rejects(webcrypto.subtle.decrypt(algorithm, key, encrypted), { name: "OperationError" });
});

test("a missing key or modified source leaves the public artifact absent", async (t) => {
  const { source, target } = await fixture(t);
  await assert.rejects(assembleEncryptedMobileSite(source, target, undefined, pwaDir), /32-byte base64url key/);
  await assert.rejects(readdir(target), { code: "ENOENT" });
  await writeFile(join(source, "mobile_snapshot.db.gz"), gzipSync("Unexpected fixture"));
  await assert.rejects(
    assembleEncryptedMobileSite(source, target, fixtureKey.toString("base64url"), pwaDir),
    /does not match its manifest/,
  );
  await assert.rejects(readdir(target), { code: "ENOENT" });
});
