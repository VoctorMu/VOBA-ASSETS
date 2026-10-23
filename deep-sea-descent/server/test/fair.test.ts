import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHmac, randomBytes } from "node:crypto";
import { DEFAULT_CONFIG, ZONES, zoneOf } from "../src/config.js";
import { outcome } from "../src/fair.js";

/** Evaluates the browser client's own provably-fair block so client and server can't drift apart. */
function loadClientFair() {
  const html = readFileSync(new URL("../../index.html", import.meta.url), "utf8");
  const start = html.indexOf("/* ---------- provably fair ---------- */");
  const end = html.indexOf("/* ---------- state ---------- */");
  assert.ok(start > 0 && end > start, "client provably-fair block not found");
  const prelude = `const HOUSE_EDGE = 0.035, MAX_MULT = 10000, SURGE_P = 0.2; const enc = new TextEncoder();
    const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");`;
  return new Function(`${prelude}\n${html.slice(start, end)}\nreturn { outcome, hmacjs, sha256js };`)() as {
    outcome: (s: string, c: string, n: number) => Promise<{ crash: number; surge: boolean; cause: string }>;
    hmacjs: (k: Uint8Array, m: Uint8Array) => Uint8Array;
    sha256js: (m: Uint8Array) => Uint8Array;
  };
}

test("server outcome matches the browser client's outcome for the same seeds", async () => {
  const client = loadClientFair();
  for (let i = 0; i < 300; i++) {
    const seed = randomBytes(32).toString("hex");
    const nonce = 1049281 + i;
    const s = outcome(seed, DEFAULT_CONFIG.publicClientSeed, nonce, DEFAULT_CONFIG);
    const c = await client.outcome(seed, DEFAULT_CONFIG.publicClientSeed, nonce);
    assert.deepEqual({ crash: c.crash, surge: c.surge, cause: c.cause }, { crash: s.crash, surge: s.surge, cause: s.cause });
  }
});

test("client's pure-JS HMAC fallback matches node:crypto", () => {
  const { hmacjs } = loadClientFair();
  const enc = new TextEncoder();
  for (const key of ["k", "x".repeat(64), "y".repeat(130), randomBytes(32).toString("hex")]) {
    const msg = "client:" + key.length;
    assert.equal(Buffer.from(hmacjs(enc.encode(key), enc.encode(msg))).toString("hex"), createHmac("sha256", key).update(msg).digest("hex"));
  }
});

test("outcome is deterministic and sensitive to every input", () => {
  const a = outcome("seed", "client", 1, DEFAULT_CONFIG);
  assert.deepEqual(outcome("seed", "client", 1, DEFAULT_CONFIG), a);
  assert.notEqual(outcome("seed", "client", 2, DEFAULT_CONFIG).hmac, a.hmac);
  assert.notEqual(outcome("seed2", "client", 1, DEFAULT_CONFIG).hmac, a.hmac);
  assert.notEqual(outcome("seed", "client2", 1, DEFAULT_CONFIG).hmac, a.hmac);
});

test("crash distribution follows P(x) = 0.965 / x", () => {
  const N = 200_000;
  const seed = randomBytes(32).toString("hex");
  let instant = 0, two = 0, ten = 0, surge = 0, max = 0;
  for (let n = 0; n < N; n++) {
    const o = outcome(seed, "dist", n, DEFAULT_CONFIG);
    if (o.crash === 1) instant++;
    if (o.crash >= 2) two++;
    if (o.crash >= 10) ten++;
    if (o.surge) surge++;
    max = Math.max(max, o.crash);
  }
  // 3.5% implode before any climb (r < e), plus dives flooring into [1.00, 1.01): 1.00x shows 1 − 0.965/1.01 ≈ 4.46% of the time
  assert.ok(Math.abs(instant / N - (1 - 0.965 / 1.01)) < 0.0025, `1.00x rate ${instant / N}`);
  assert.ok(Math.abs(two / N - 0.4825) < 0.005, `P(>=2) ${two / N}`);
  assert.ok(Math.abs(ten / N - 0.0965) < 0.003, `P(>=10) ${ten / N}`);
  assert.ok(Math.abs(surge / N - 0.2) < 0.005, `surge rate ${surge / N}`);
  assert.ok(max <= DEFAULT_CONFIG.maxMultiplier);
});

test("side-bet payouts keep RTP at or below 95.5% including Surge doubling", () => {
  const reach = (x: number) => (x <= 1 ? 1 : 0.965 / x);
  for (const z of ZONES) {
    const p = reach(z.lo) - (z.hi === Infinity ? 0 : reach(z.hi));
    const rtp = p * z.pay * (0.8 + 0.2 * 2);
    assert.ok(rtp <= 0.955 && rtp > 0.945, `${z.id} RTP ${rtp}`);
  }
  assert.equal(zoneOf(1).id, "sun");
  assert.equal(zoneOf(1.99).id, "sun");
  assert.equal(zoneOf(2).id, "twi");
  assert.equal(zoneOf(99.99).id, "mid");
  assert.equal(zoneOf(100).id, "aby");
});
