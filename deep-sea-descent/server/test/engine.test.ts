import { test } from "node:test";
import assert from "node:assert/strict";
import { GameEngine, GameError } from "../src/engine.js";
import { outcome } from "../src/fair.js";
import { InMemoryWallet, type WalletRequest } from "../src/wallet.js";
import type { GameConfig } from "../src/config.js";

type Fixed = { crash: number; surge?: boolean };
const flush = () => new Promise((r) => setImmediate(r));

function setup(fixed: Fixed | null, config: Partial<GameConfig> = {}, wallet?: InMemoryWallet) {
  let now = Date.UTC(2026, 9, 23, 12);
  const clock = () => now;
  const w = wallet ?? new InMemoryWallet(1000, "EUR", clock);
  const engine = new GameEngine({
    wallet: w,
    clock,
    config: { botsMin: 0, botsMax: 0, ...config },
    outcomeFn: fixed ? () => ({ hmac: "", crash: fixed.crash, surge: !!fixed.surge, cause: "hull" }) : undefined,
  });
  const direct: { pid: string; msg: any }[] = [];
  const broadcast: any[] = [];
  engine.on("direct", (pid, msg) => direct.push({ pid, msg }));
  engine.on("broadcast", (msg) => broadcast.push(msg));
  engine.start();
  return {
    engine,
    wallet: w,
    direct,
    broadcast,
    /** move the clock forward and run one engine step */
    advance(ms: number) {
      now += ms;
      engine.step();
    },
    /** move the clock without stepping, as if the request arrived between ticks */
    jump(ms: number) {
      now += ms;
    },
    toDescent() {
      this.advance(8000);
      this.advance(2000);
      assert.equal(engine.phase, "descent");
    },
  };
}

const code = async (p: Promise<unknown> | (() => unknown), expected: string) => {
  await assert.rejects(async () => (typeof p === "function" ? p() : p), (e: unknown) => e instanceof GameError && e.code === expected);
};

test("manual cash-out pays stake × executed multiplier and credits the wallet", async () => {
  const g = setup({ crash: 5 });
  const bet = await g.engine.placeBet("usr_aaaaaa", "A", 10, null);
  assert.equal(bet.balance, 990);
  g.toDescent();
  g.advance(4400); // e^(0.16·4.4) = 2.0219…
  const ack = await g.engine.cashout("usr_aaaaaa", "A", 123);
  assert.equal(ack.action, "cashout_ack");
  assert.equal(ack.status, "SUCCESS");
  assert.equal(ack.panel, "PANEL_A");
  assert.equal(ack.multiplier_executed, 2.02);
  assert.equal(ack.payout_amount, 20.2);
  assert.equal(ack.client_timestamp, 123);
  assert.equal(await g.wallet.balance("usr_aaaaaa"), 1010.2);
  await code(g.engine.cashout("usr_aaaaaa", "A"), "ALREADY_CASHED");
});

test("a cash-out that arrives after the crash point is refused", async () => {
  const g = setup({ crash: 1.5 });
  await g.engine.placeBet("usr_aaaaaa", "A", 10, null);
  g.toDescent();
  g.jump(5000); // crash was due at ln(1.5)/0.16 ≈ 2.53 s, but no tick has run yet
  await code(g.engine.cashout("usr_aaaaaa", "A"), "TOO_LATE");
  assert.equal(g.engine.phase, "crashed");
  assert.equal(await g.wallet.balance("usr_aaaaaa"), 990);
});

test("auto cash-out pays exactly at the target, and loses when the dive implodes first", async () => {
  const win = setup({ crash: 3 });
  await win.engine.placeBet("usr_aaaaaa", "A", 10, 2.5);
  win.toDescent();
  win.advance(7000);
  await flush();
  const ack = win.direct.find((d) => d.msg.action === "cashout_ack")?.msg;
  assert.equal(ack.multiplier_executed, 2.5);
  assert.equal(ack.payout_amount, 25);
  assert.equal(ack.mode, "auto");

  const lose = setup({ crash: 2 });
  await lose.engine.placeBet("usr_aaaaaa", "A", 10, 2.5);
  lose.toDescent();
  lose.advance(10000);
  await flush();
  assert.ok(!lose.direct.some((d) => d.msg.action === "cashout_ack"));
  assert.ok(lose.direct.some((d) => d.msg.action === "bet_lost"));
  assert.equal(await lose.wallet.balance("usr_aaaaaa"), 990);
});

test("a 1.00x dive implodes immediately and pays nobody", async () => {
  const g = setup({ crash: 1 });
  await g.engine.placeBet("usr_aaaaaa", "A", 10, 1.01);
  g.toDescent();
  g.advance(20);
  assert.equal(g.engine.phase, "crashed");
  await flush();
  assert.ok(!g.direct.some((d) => d.msg.action === "cashout_ack"));
});

test("bet validation: window, duplicates, limits and funds", async () => {
  const g = setup({ crash: 5 });
  await code(g.engine.placeBet("usr_aaaaaa", "A", Number.NaN, null), "BAD_AMOUNT");
  await code(g.engine.placeBet("usr_aaaaaa", "A", 0.05, null), "BAD_AMOUNT");
  await code(g.engine.placeBet("usr_aaaaaa", "A", 1000.01, null), "BAD_AMOUNT");
  await g.engine.placeBet("usr_aaaaaa", "A", 1000, null);
  await code(g.engine.placeBet("usr_aaaaaa", "A", 1, null), "ALREADY_BET");
  await code(g.engine.placeBet("usr_aaaaaa", "B", 0.5, null), "INSUFFICIENT_FUNDS");
  g.advance(8000);
  await code(g.engine.placeBet("usr_bbbbbb", "A", 1, null), "BETTING_CLOSED");
  await code(g.engine.cancelBet("usr_aaaaaa", "A"), "BETTING_CLOSED");
});

test("cancelling during the betting window refunds through a rollback", async () => {
  const g = setup({ crash: 5 });
  await g.engine.placeBet("usr_aaaaaa", "B", 25, null);
  const ack = await g.engine.cancelBet("usr_aaaaaa", "B");
  assert.equal(ack.balance, 1000);
  assert.equal(g.wallet.log("usr_aaaaaa")[0]!.endpoint, "POST /wallet/rollback");
});

test("a debit still in flight when betting closes is refunded, not ridden", async () => {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  class SlowWallet extends InMemoryWallet {
    override async debit(req: WalletRequest) {
      await gate;
      return super.debit(req);
    }
  }
  const g = setup({ crash: 5 }, {}, new SlowWallet(1000, "EUR"));
  const pending = g.engine.placeBet("usr_aaaaaa", "A", 10, null);
  g.advance(8000); // launch while the wallet is still thinking
  release();
  await code(pending, "BETTING_CLOSED");
  assert.equal(await g.wallet.balance("usr_aaaaaa"), 1000);
});

test("side bets settle on the crash zone and double during a Pressure Surge", async () => {
  const g = setup({ crash: 3.2, surge: true });
  await g.engine.sideBet("usr_aaaaaa", "twi", 10);
  await g.engine.sideBet("usr_aaaaaa", "sun", 5);
  await code(g.engine.sideBet("usr_aaaaaa", "aby", 501), "BAD_AMOUNT");
  await code(g.engine.sideBet("usr_aaaaaa", "nope" as never, 1), "BAD_ZONE");
  g.toDescent();
  g.advance(20000);
  await flush();
  const res = g.direct.find((d) => d.msg.action === "side_result")?.msg;
  assert.equal(res.zone, "twi");
  assert.equal(res.multiplier, 4.12);
  assert.equal(res.win, 41.2);
  assert.equal(await g.wallet.balance("usr_aaaaaa"), 1026.2);
});

test("payouts are capped at the per-bet maximum", async () => {
  const g = setup({ crash: 400 }, { maxPayout: 250000 }, new InMemoryWallet(5000, "EUR"));
  await g.engine.placeBet("usr_aaaaaa", "A", 1000, 300);
  g.toDescent();
  g.advance(40000);
  await flush();
  const ack = g.direct.find((d) => d.msg.action === "cashout_ack")?.msg;
  assert.equal(ack.payout_amount, 250000);
});

test("the 500x jackpot is shared among players still riding", async () => {
  const g = setup({ crash: 600 });
  const pool = g.engine.jackpot;
  await g.engine.placeBet("usr_aaaaaa", "A", 10, null);
  g.toDescent();
  g.advance(Math.ceil((Math.log(500) / 0.16) * 1000) + 10);
  await flush();
  const share = g.direct.find((d) => d.msg.action === "jackpot_share")?.msg;
  assert.ok(share, "no jackpot share");
  assert.ok(Math.abs(share.amount - (pool + 10 * 0.004)) < 0.011, `share ${share.amount}`);
  assert.equal(g.engine.jackpot, 5000);
});

test("seeds stay hidden until the implosion, then verify against the HMAC", async () => {
  const g = setup(null);
  const before = g.engine.publicRound();
  assert.ok(!("crash" in before) && !("surge" in before) && !("server_seed" in before));
  const roundId = g.engine.round.id;
  assert.equal(g.engine.verify(roundId), undefined);
  g.toDescent();
  for (let i = 0; i < 400 && g.engine.phase === "descent"; i++) g.advance(250);
  const rec = g.engine.verify(roundId)!;
  assert.ok(rec, "round not revealed");
  assert.equal(rec.server_seed_hash, before.server_seed_hash);
  assert.equal(outcome(rec.server_seed, rec.client_seed, rec.nonce, g.engine.cfg).crash, rec.result_multiplier);
});

test("daily wheel spins once per player per day; Sonar Rain needs recent activity", async () => {
  const g = setup({ crash: 2 });
  const spin = await g.engine.wheelSpin("usr_aaaaaa");
  assert.equal(spin.balance, 1000 + (spin.amount as number));
  await code(g.engine.wheelSpin("usr_aaaaaa"), "WHEEL_USED");

  // rain starts on the 4th betting window
  for (let i = 0; i < 3; i++) {
    g.toDescent();
    g.advance(20000);
    g.advance(3000);
  }
  const rain = g.broadcast.find((m) => m.action === "sonar_rain");
  assert.ok(rain, "no rain");
  await code(g.engine.catchRain("usr_aaaaaa", rain.id), "RAIN_NOT_QUALIFIED");
  g.engine.chat("usr_aaaaaa", "hello bridge");
  const ack = await g.engine.catchRain("usr_aaaaaa", rain.id);
  assert.equal(ack.amount, rain.amount);
  await code(g.engine.catchRain("usr_aaaaaa", rain.id), "RAIN_CAUGHT");
  assert.throws(() => g.engine.chat("usr_aaaaaa", "again"), (e: unknown) => e instanceof GameError && e.code === "RATE_LIMIT");
});
