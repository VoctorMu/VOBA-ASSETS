export type ZoneId = "sun" | "twi" | "mid" | "aby";

export interface Zone {
  id: ZoneId;
  name: string;
  lo: number;
  hi: number;
  pay: number;
}

export interface GameConfig {
  houseEdge: number;
  maxMultiplier: number;
  maxPayout: number;
  tableMin: number;
  tableMax: number;
  sideMaxPerZone: number;
  betMs: number;
  launchMs: number;
  crashMs: number;
  /** multiplier(t) = e^(k·t), t in seconds; doubled during a Pressure Surge */
  kBase: number;
  surgeProbability: number;
  jackpotSeed: number;
  jackpotTrigger: number;
  /** share of each dive's betting volume added to the operator-funded jackpot */
  jackpotRate: number;
  tickMs: number;
  currency: string;
  startingBalance: number;
  /** public client seed shared by every player, since all players ride the same dive */
  publicClientSeed: string;
  botsMin: number;
  botsMax: number;
  zones: Zone[];
}

// Side-bet payouts solved from 1.2 · p · payout <= 0.955 (Surge doubles payouts in 1 of 5 dives).
// Keep in step with ZONES in ../index.html.
export const ZONES: Zone[] = [
  { id: "sun", name: "Sunlight Zone", lo: 1, hi: 2, pay: 1.53 },
  { id: "twi", name: "Twilight Zone", lo: 2, hi: 10, pay: 2.06 },
  { id: "mid", name: "Midnight Zone", lo: 10, hi: 100, pay: 9.16 },
  { id: "aby", name: "Abyssal Trench", lo: 100, hi: Infinity, pay: 82.4 },
];

export const DEFAULT_CONFIG: GameConfig = {
  houseEdge: 0.035,
  maxMultiplier: 10000,
  maxPayout: 250000,
  tableMin: 0.1,
  tableMax: 1000,
  sideMaxPerZone: 500,
  betMs: 8000,
  launchMs: 2000,
  crashMs: 3000,
  kBase: 0.16,
  surgeProbability: 0.2,
  jackpotSeed: 5000,
  jackpotTrigger: 500,
  jackpotRate: 0.004,
  tickMs: 250,
  currency: "EUR",
  startingBalance: 1000,
  publicClientSeed: "000000000000000000042d38e211832049e2cf31",
  botsMin: 22,
  botsMax: 60,
  zones: ZONES,
};

export function zoneOf(m: number, zones: Zone[] = ZONES): Zone {
  return zones.find((z) => m >= z.lo && m < z.hi) ?? zones[0]!;
}

export const cents = (v: number): number => Math.round(v * 100) / 100;
export const floor2 = (v: number): number => Math.floor(v * 100 + 1e-9) / 100;
