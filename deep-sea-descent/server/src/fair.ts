import { createHash, createHmac, randomBytes } from "node:crypto";

export interface Outcome {
  hmac: string;
  crash: number;
  surge: boolean;
  cause: "leviathan" | "hull";
}

export const sha256Hex = (s: string): string => createHash("sha256").update(s).digest("hex");

export const newServerSeed = (): string => randomBytes(32).toString("hex");

/**
 * HMAC_SHA256(serverSeed, clientSeed + ":" + nonce).
 * The first 52 bits give r in [0,1); crash = floor(100·(1−e)/(1−r))/100, so P(crash ≥ x) = (1−e)/x
 * and r < e implodes at 1.00x. Must stay byte-for-byte identical to outcome() in ../index.html.
 */
export function outcome(
  serverSeed: string,
  clientSeed: string,
  nonce: number,
  opts: { houseEdge: number; maxMultiplier: number; surgeProbability: number },
): Outcome {
  const h = createHmac("sha256", serverSeed).update(clientSeed + ":" + nonce).digest("hex");
  const r = parseInt(h.slice(0, 13), 16) / 2 ** 52;
  const crash = Math.min(opts.maxMultiplier, Math.max(1, Math.floor((100 * (1 - opts.houseEdge)) / (1 - r)) / 100));
  const surge = parseInt(h.slice(16, 24), 16) / 2 ** 32 < opts.surgeProbability;
  const cause = parseInt(h.slice(24, 26), 16) % 3 === 0 ? "leviathan" : "hull";
  return { hmac: h, crash, surge, cause };
}
