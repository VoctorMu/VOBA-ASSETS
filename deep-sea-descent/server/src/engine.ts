import { EventEmitter } from "node:events";
import { randomInt } from "node:crypto";
import { DEFAULT_CONFIG, cents, floor2, zoneOf, type GameConfig, type ZoneId } from "./config.js";
import { newServerSeed, outcome, sha256Hex, type Outcome } from "./fair.js";
import { InsufficientFundsError, type Wallet, type WalletRequest } from "./wallet.js";

export type Phase = "betting" | "launch" | "descent" | "crashed";
export type PanelId = "A" | "B";
type BetState = "pending" | "placed" | "cashed" | "lost";
type Msg = Record<string, unknown> & { action: string };

interface Bet {
  pid: string;
  name: string;
  panel: PanelId;
  amount: number;
  auto: number | null;
  state: BetState;
  multiplier?: number;
  payout?: number;
}

interface Bot {
  name: string;
  stake: number;
  target: number;
  out: number;
  win: number;
  lost: boolean;
}

interface Round {
  id: string;
  nonce: number;
  serverSeed: string;
  serverSeedHash: string;
  clientSeed: string;
  crash: number;
  surge: boolean;
  cause: Outcome["cause"];
  k: number;
  jackpotHit: boolean;
}

interface Rain {
  id: string;
  amount: number;
  remaining: number;
  expiresAt: number;
  caught: Set<string>;
}

export interface FairRecord {
  round_id: string;
  server_seed: string;
  server_seed_hash: string;
  client_seed: string;
  nonce: number;
  result_multiplier: number;
  surge: boolean;
}

export class GameError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export interface EngineOptions {
  wallet: Wallet;
  config?: Partial<GameConfig>;
  clock?: () => number;
  random?: () => number;
  /** override for tests; defaults to the provably-fair HMAC outcome */
  outcomeFn?: (serverSeed: string, clientSeed: string, nonce: number) => Outcome;
}

const EMPTY_SIDES = (): Record<ZoneId, number> => ({ sun: 0, twi: 0, mid: 0, aby: 0 });
export const WHEEL = [5, 10, 2, 25, 5, 50, 10, 100];
export const REACTIONS = ["🫧", "🐙", "🦑", "🐋", "🐡", "🔥", "😱"];
const RAIN_MS = 15000;
const ACTIVE_WINDOW_MS = 180000;
const BOT_NAMES = ["Player99", "NautilusNed", "Kraken_Kate", "deepdiver_88", "Marlin", "AbyssWalker", "Sonar_Sam", "BubbleBaron", "Hadal_Hana", "TrenchRat", "Coral_Q", "Anglerfish", "Leviathan_Lou", "MarianaM", "BenthicBen", "SiphonSue", "Nemo_2", "PelagicPete", "VentHunter", "Jelly_J", "Bathy_B", "SnailfishSi", "OarfishO", "Lanternfish", "Gulper_G", "TideTurner", "Pressure_P", "Isopod_Ivy", "Abyss_Al", "Hydro_H", "Siphono", "Dumbo_Oct", "BlueHole", "Mid_Water", "Ballast_B", "Sub_Zero", "Fathom5", "Krill_K", "Riftia", "Chimaera"];
const BOT_LINES: Record<"betting" | "descent", string[]> = {
  betting: ["A on 1.4x, B riding to the trench", "feeling an abyssal one", "Twilight side bet again", "Captain, take us deep", "who's in for 100x?", "small stakes this round"],
  descent: ["hold…", "HOLD", "sonar pinging fast", "still in!", "past the twilight line 👀", "come on 10x"],
};

/**
 * Server-authoritative round engine. All outcomes, timing and settlement happen here;
 * clients only render. Call step() on a short interval (the server uses 20 ms).
 * Emits "broadcast" (msg) for every client and "direct" (playerId, msg) for one player.
 */
export class GameEngine extends EventEmitter {
  readonly cfg: GameConfig;
  phase: Phase = "betting";
  phaseStart = 0;
  round!: Round;
  bots: Bot[] = [];
  history: number[] = [];
  jackpot: number;

  private readonly wallet: Wallet;
  private readonly clock: () => number;
  private readonly random: () => number;
  private readonly outcomeFn: NonNullable<EngineOptions["outcomeFn"]>;
  private roundNo = 1049280;
  private bets = new Map<string, Bet>();
  private sides = new Map<string, Record<ZoneId, number>>();
  private fair = new Map<string, FairRecord>();
  private names = new Map<string, string>();
  private lastActive = new Map<string, number>();
  private lastChat = new Map<string, number>();
  private lastReaction = new Map<string, number>();
  private wheelSpins = new Map<string, string>();
  private rain: Rain | null = null;
  private rainIn = 4;
  private lastTick = 0;
  private crewDirty = true;
  private lastCrew = 0;
  private nextBotChat = 0;

  constructor(opts: EngineOptions) {
    super();
    this.cfg = { ...DEFAULT_CONFIG, ...opts.config };
    this.wallet = opts.wallet;
    this.clock = opts.clock ?? Date.now;
    this.random = opts.random ?? Math.random;
    this.outcomeFn = opts.outcomeFn ?? ((s, c, n) => outcome(s, c, n, this.cfg));
    this.jackpot = 18450 + Math.floor(this.random() * 4000);
  }

  start(): void {
    this.startBetting(this.clock());
  }

  /* ---------- clock-driven state machine ---------- */

  step(): void {
    const now = this.clock();
    const t = now - this.phaseStart;
    if (this.phase === "betting" && t >= this.cfg.betMs) this.startLaunch(now);
    else if (this.phase === "launch" && t >= this.cfg.launchMs) this.startDescent(now);
    else if (this.phase === "descent") {
      const raw = this.multiplierAt(now);
      this.processAutos(Math.min(raw, this.round.crash));
      if (!this.round.jackpotHit && this.round.crash >= this.cfg.jackpotTrigger && raw >= this.cfg.jackpotTrigger) this.payJackpot();
      if (raw >= this.round.crash) this.crash(now);
      else if (now - this.lastTick >= this.cfg.tickMs) {
        this.lastTick = now;
        this.broadcast({ action: "tick", round_id: this.round.id, phase_elapsed_ms: t, multiplier: floor2(raw), server_timestamp: now });
      }
    } else if (this.phase === "crashed" && t >= this.cfg.crashMs) this.startBetting(now);

    if (this.crewDirty && now - this.lastCrew >= 300) {
      this.crewDirty = false;
      this.lastCrew = now;
      this.broadcast({ action: "crew", round_id: this.round.id, crew: this.crewList() });
    }
    this.botActivity(now);
  }

  multiplierAt(now: number): number {
    return Math.exp((this.round.k * (now - this.phaseStart)) / 1000);
  }

  private startBetting(now: number): void {
    this.phase = "betting";
    this.phaseStart = now;
    this.bets.clear();
    this.sides.clear();
    this.roundNo++;
    const serverSeed = newServerSeed();
    const clientSeed = this.cfg.publicClientSeed;
    const o = this.outcomeFn(serverSeed, clientSeed, this.roundNo);
    this.round = {
      id: "rnd_" + this.roundNo,
      nonce: this.roundNo,
      serverSeed,
      serverSeedHash: sha256Hex(serverSeed),
      clientSeed,
      crash: o.crash,
      surge: o.surge,
      cause: o.cause,
      k: this.cfg.kBase * (o.surge ? 2 : 1),
      jackpotHit: false,
    };
    this.spawnBots();
    this.crewDirty = true;
    this.broadcast({ action: "round_betting", ...this.publicRound(now) });
    if (--this.rainIn <= 0) {
      this.rainIn = 5 + Math.floor(this.random() * 4);
      this.startRain(now);
    }
  }

  private startLaunch(now: number): void {
    this.phase = "launch";
    this.phaseStart = now;
    let volume = this.bots.reduce((a, b) => a + b.stake, 0);
    for (const b of this.bets.values()) if (b.state === "placed") volume += b.amount;
    this.jackpot = cents(this.jackpot + volume * this.cfg.jackpotRate);
    this.broadcast({ action: "round_launch", ...this.publicRound(now) });
  }

  private startDescent(now: number): void {
    this.phase = "descent";
    this.phaseStart = now;
    this.lastTick = now;
    this.broadcast({ action: "round_descent", ...this.publicRound(now) });
  }

  private crash(now: number): void {
    const r = this.round;
    this.phase = "crashed";
    this.phaseStart = now;
    for (const b of this.bets.values()) {
      if (b.state !== "placed") continue;
      b.state = "lost";
      this.direct(b.pid, { action: "bet_lost", panel: "PANEL_" + b.panel, round_id: r.id, amount: b.amount, crash: r.crash });
    }
    for (const bot of this.bots) if (!bot.out) bot.lost = true;
    const zone = zoneOf(r.crash, this.cfg.zones);
    for (const [pid, s] of this.sides) {
      const stake = s[zone.id];
      const multiplier = zone.pay * (r.surge ? 2 : 1);
      const total = cents(Object.values(s).reduce((a, v) => a + v, 0));
      if (stake > 0) {
        void this.walletOp("credit", { player_id: pid, round_id: r.id, amount: cents(stake * multiplier), side_bet: zone.id, multiplier })
          .then((res) => this.direct(pid, { action: "side_result", round_id: r.id, zone: zone.id, staked: total, win: res.payload.amount, multiplier, balance: res.balance }))
          .catch((e) => this.walletFailure(pid, e));
      } else {
        this.direct(pid, { action: "side_result", round_id: r.id, zone: zone.id, staked: total, win: 0, multiplier });
      }
    }
    this.history.unshift(r.crash);
    if (this.history.length > 24) this.history.pop();
    this.fair.set(r.id, {
      round_id: r.id,
      server_seed: r.serverSeed,
      server_seed_hash: r.serverSeedHash,
      client_seed: r.clientSeed,
      nonce: r.nonce,
      result_multiplier: r.crash,
      surge: r.surge,
    });
    if (this.fair.size > 500) this.fair.delete(this.fair.keys().next().value!);
    this.crewDirty = true;
    this.broadcast({
      action: "round_crash",
      ...this.publicRound(now),
      zone: zone.id,
      server_seed: r.serverSeed,
      history: this.history,
    });
  }

  private processAutos(m: number): void {
    for (const b of this.bets.values()) {
      if (b.state === "placed" && b.auto !== null && b.auto <= m && b.auto <= this.round.crash) void this.settleCashout(b, b.auto, "auto").catch(() => {});
    }
    for (const bot of this.bots) {
      if (bot.out || bot.target > m || bot.target > this.round.crash) continue;
      bot.out = bot.target;
      bot.win = cents(Math.min(bot.stake * bot.target, this.cfg.maxPayout));
      this.crewDirty = true;
      this.broadcast({ action: "cashout_event", name: bot.name, multiplier: bot.out, payout: bot.win });
    }
  }

  private payJackpot(): void {
    this.round.jackpotHit = true;
    const riders: { pid?: string; stake: number }[] = [];
    for (const b of this.bets.values()) if (b.state === "placed") riders.push({ pid: b.pid, stake: b.amount });
    for (const bot of this.bots) if (!bot.out) riders.push({ stake: bot.stake });
    if (!riders.length) return;
    const pool = this.jackpot;
    const total = riders.reduce((a, r) => a + r.stake, 0);
    const shares = new Map<string, number>();
    for (const r of riders) if (r.pid) shares.set(r.pid, (shares.get(r.pid) ?? 0) + (pool * r.stake) / total);
    for (const [pid, amount] of shares) {
      void this.walletOp("credit", { player_id: pid, round_id: this.round.id, amount: cents(amount), type: "jackpot_drop" })
        .then((res) => this.direct(pid, { action: "jackpot_share", round_id: this.round.id, amount: res.payload.amount, balance: res.balance }))
        .catch((e) => this.walletFailure(pid, e));
    }
    this.jackpot = this.cfg.jackpotSeed;
    this.broadcast({ action: "jackpot_drop", round_id: this.round.id, pool, riders: riders.length, jackpot: this.jackpot });
  }

  /* ---------- player actions (each resolves to an ack or throws GameError) ---------- */

  setName(pid: string, name: string): void {
    this.names.set(pid, name);
  }

  async placeBet(pid: string, panel: PanelId, amount: number, auto: number | null): Promise<Msg> {
    if (this.phase !== "betting") throw new GameError("BETTING_CLOSED", "Bets are closed for this dive. Queue it for the next one.");
    amount = this.validStake(amount, this.cfg.tableMax);
    auto = this.validAuto(auto);
    const key = pid + "|" + panel;
    if (this.bets.has(key)) throw new GameError("ALREADY_BET", `Panel ${panel} already has a bet this dive.`);
    const roundId = this.round.id;
    const bet: Bet = { pid, name: this.nameOf(pid), panel, amount, auto, state: "pending" };
    this.bets.set(key, bet);
    let res;
    try {
      res = await this.walletOp("debit", { player_id: pid, round_id: roundId, amount, panel: "PANEL_" + panel });
    } catch (e) {
      if (this.bets.get(key) === bet) this.bets.delete(key);
      throw this.asGameError(e);
    }
    if (this.bets.get(key) !== bet || this.phase !== "betting" || this.round.id !== roundId) {
      if (this.bets.get(key) === bet) this.bets.delete(key);
      await this.walletOp("rollback", { player_id: pid, round_id: roundId, amount, panel: "PANEL_" + panel });
      throw new GameError("BETTING_CLOSED", "The betting window closed before the bet was confirmed. It has been refunded.");
    }
    bet.state = "placed";
    this.crewDirty = true;
    return { status: "SUCCESS", action: "bet_ack", panel: "PANEL_" + panel, round_id: roundId, amount, auto_cashout: auto, balance: res.balance };
  }

  async cancelBet(pid: string, panel: PanelId): Promise<Msg> {
    const key = pid + "|" + panel;
    const bet = this.bets.get(key);
    if (this.phase !== "betting") throw new GameError("BETTING_CLOSED", "Bets are locked once the dive launches.");
    if (!bet || bet.state !== "placed") throw new GameError("NO_BET", `Panel ${panel} has no bet to cancel.`);
    this.bets.delete(key);
    this.crewDirty = true;
    const res = await this.walletOp("rollback", { player_id: pid, round_id: this.round.id, amount: bet.amount, panel: "PANEL_" + panel });
    return { status: "SUCCESS", action: "cancel_ack", panel: "PANEL_" + panel, round_id: this.round.id, amount: bet.amount, balance: res.balance };
  }

  setAuto(pid: string, panel: PanelId, auto: number | null): Msg {
    const bet = this.bets.get(pid + "|" + panel);
    if (!bet || (bet.state !== "placed" && bet.state !== "pending")) throw new GameError("NO_BET", `Panel ${panel} has no active bet.`);
    bet.auto = this.validAuto(auto);
    return { status: "SUCCESS", action: "auto_ack", panel: "PANEL_" + panel, round_id: this.round.id, auto_cashout: bet.auto };
  }

  async cashout(pid: string, panel: PanelId, clientTimestamp?: number): Promise<Msg> {
    const roundId = this.round.id;
    if (this.phase === "descent") this.step(); // settle a crash that is already due before honouring the request
    const bet = this.bets.get(pid + "|" + panel);
    if (this.phase !== "descent" || this.round.id !== roundId) {
      throw new GameError(bet?.state === "lost" ? "TOO_LATE" : "ROUND_NOT_ACTIVE", bet?.state === "lost" ? "The hull imploded before the cash-out arrived." : "There is no descent in progress.");
    }
    if (!bet || bet.state !== "placed") throw new GameError(bet?.state === "cashed" ? "ALREADY_CASHED" : "NO_BET", bet?.state === "cashed" ? `Panel ${panel} already cashed out.` : `Panel ${panel} has no bet riding.`);
    const ack = await this.settleCashout(bet, floor2(this.multiplierAt(this.clock())), "manual");
    return clientTimestamp ? { ...ack, client_timestamp: clientTimestamp } : ack;
  }

  async sideBet(pid: string, zone: ZoneId, amount: number): Promise<Msg> {
    if (this.phase !== "betting") throw new GameError("BETTING_CLOSED", "Side bets lock when the dive launches.");
    if (!this.cfg.zones.some((z) => z.id === zone)) throw new GameError("BAD_ZONE", "Unknown depth zone.");
    amount = this.validStake(amount, this.cfg.sideMaxPerZone);
    const s = this.sides.get(pid) ?? EMPTY_SIDES();
    if (s[zone] + amount > this.cfg.sideMaxPerZone) throw new GameError("SIDE_LIMIT", `Side-bet maximum is €${this.cfg.sideMaxPerZone} per zone.`);
    const roundId = this.round.id;
    s[zone] = cents(s[zone] + amount); // reserve before the async debit so concurrent bets respect the cap
    this.sides.set(pid, s);
    let res;
    try {
      res = await this.walletOp("debit", { player_id: pid, round_id: roundId, amount, side_bet: zone });
    } catch (e) {
      s[zone] = cents(s[zone] - amount);
      throw this.asGameError(e);
    }
    if (this.phase !== "betting" || this.round.id !== roundId) {
      s[zone] = cents(s[zone] - amount);
      await this.walletOp("rollback", { player_id: pid, round_id: roundId, amount, side_bet: zone });
      throw new GameError("BETTING_CLOSED", "The betting window closed before the side bet was confirmed. It has been refunded.");
    }
    return { status: "SUCCESS", action: "side_ack", round_id: roundId, zone, amount, sides: { ...s }, balance: res.balance };
  }

  async sideClear(pid: string): Promise<Msg> {
    if (this.phase !== "betting") throw new GameError("BETTING_CLOSED", "Side bets are locked for this dive.");
    const s = this.sides.get(pid);
    const total = s ? cents(Object.values(s).reduce((a, v) => a + v, 0)) : 0;
    this.sides.delete(pid);
    const balance = total > 0 ? (await this.walletOp("rollback", { player_id: pid, round_id: this.round.id, amount: total, side_bet: "all" })).balance : await this.wallet.balance(pid);
    return { status: "SUCCESS", action: "side_ack", round_id: this.round.id, sides: EMPTY_SIDES(), balance };
  }

  chat(pid: string, text: string): void {
    const now = this.clock();
    if (now - (this.lastChat.get(pid) ?? -Infinity) < 700) throw new GameError("RATE_LIMIT", "Slow down a little between messages.");
    const clean = String(text).replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 120);
    if (!clean) throw new GameError("EMPTY", "Type a message first.");
    this.lastChat.set(pid, now);
    this.lastActive.set(pid, now);
    this.broadcast({ action: "chat", pid, name: this.nameOf(pid), text: clean });
  }

  reaction(pid: string, emoji: string): void {
    const now = this.clock();
    if (!REACTIONS.includes(emoji)) throw new GameError("BAD_REACTION", "Unknown reaction.");
    if (now - (this.lastReaction.get(pid) ?? -Infinity) < 250) return; // drop floods silently
    this.lastReaction.set(pid, now);
    this.lastActive.set(pid, now);
    this.broadcast({ action: "reaction", pid, emoji });
  }

  async catchRain(pid: string, id: string): Promise<Msg> {
    const r = this.rain;
    const now = this.clock();
    if (!r || r.id !== id || now > r.expiresAt || r.remaining <= 0) throw new GameError("RAIN_OVER", "That Sonar Rain is over.");
    if (r.caught.has(pid)) throw new GameError("RAIN_CAUGHT", "You already caught a free bet from this drop.");
    if (now - (this.lastActive.get(pid) ?? -Infinity) > ACTIVE_WINDOW_MS) throw new GameError("RAIN_NOT_QUALIFIED", "Send a chat message or a reaction to qualify, then tap again.");
    r.caught.add(pid);
    r.remaining--;
    const res = await this.walletOp("credit", { player_id: pid, round_id: this.round.id, amount: r.amount, type: "sonar_rain_voucher" });
    return { status: "SUCCESS", action: "rain_ack", id, amount: r.amount, balance: res.balance };
  }

  async wheelSpin(pid: string): Promise<Msg> {
    const day = new Date(this.clock()).toISOString().slice(0, 10);
    if (this.wheelSpins.get(pid) === day) throw new GameError("WHEEL_USED", "Today's Oxygen Tank spin is used. It refills at midnight UTC.");
    this.wheelSpins.set(pid, day);
    const index = randomInt(WHEEL.length);
    const res = await this.walletOp("credit", { player_id: pid, round_id: this.round.id, amount: WHEEL[index]!, type: "oxygen_wheel_bonus" });
    return { status: "SUCCESS", action: "wheel_result", index, amount: WHEEL[index], balance: res.balance };
  }

  async resetWallet(pid: string): Promise<Msg> {
    const diff = cents(this.cfg.startingBalance - (await this.wallet.balance(pid)));
    let balance = this.cfg.startingBalance;
    if (diff > 0) balance = (await this.walletOp("credit", { player_id: pid, round_id: this.round.id, amount: diff, type: "demo_topup" })).balance;
    else if (diff < 0) balance = (await this.walletOp("debit", { player_id: pid, round_id: this.round.id, amount: -diff, type: "demo_reset" })).balance;
    return { status: "SUCCESS", action: "wallet_reset", balance };
  }

  /* ---------- read models ---------- */

  async snapshot(pid: string): Promise<Msg> {
    const now = this.clock();
    const panels: Record<string, unknown> = {};
    for (const b of this.bets.values()) {
      if (b.pid === pid && b.state !== "pending") panels[b.panel] = { state: b.state, amount: b.amount, auto_cashout: b.auto, multiplier: b.multiplier ?? null, payout: b.payout ?? null };
    }
    return {
      action: "welcome",
      player_id: pid,
      name: this.nameOf(pid),
      balance: await this.wallet.balance(pid),
      currency: this.cfg.currency,
      config: {
        bet_ms: this.cfg.betMs,
        launch_ms: this.cfg.launchMs,
        crash_ms: this.cfg.crashMs,
        k_base: this.cfg.kBase,
        table_min: this.cfg.tableMin,
        table_max: this.cfg.tableMax,
        side_max: this.cfg.sideMaxPerZone,
        max_payout: this.cfg.maxPayout,
        zones: this.cfg.zones.map((z) => ({ id: z.id, pay: z.pay })),
      },
      round: this.publicRound(now),
      history: this.history,
      crew: this.crewList(),
      panels,
      sides: this.sides.get(pid) ?? EMPTY_SIDES(),
      wallet_log: this.wallet.log(pid).slice(0, 20),
      wheel_used: this.wheelSpins.get(pid) === new Date(now).toISOString().slice(0, 10),
      rain: this.rain && now < this.rain.expiresAt && this.rain.remaining > 0 ? { id: this.rain.id, amount: this.rain.amount, count: this.rain.remaining, expires_in_ms: this.rain.expiresAt - now } : null,
    };
  }

  verify(roundId: string): FairRecord | undefined {
    return this.fair.get(roundId);
  }

  /** what every client may know about the current dive; the crash point and seed stay hidden until the implosion */
  publicRound(now = this.clock()): Record<string, unknown> {
    const r = this.round;
    const revealed = this.phase !== "betting";
    return {
      round_id: r.id,
      nonce: r.nonce,
      phase: this.phase,
      phase_elapsed_ms: now - this.phaseStart,
      server_seed_hash: r.serverSeedHash,
      client_seed: r.clientSeed,
      jackpot: this.jackpot,
      ...(revealed ? { surge: r.surge, k: r.k } : {}),
      ...(this.phase === "crashed" ? { crash: r.crash, cause: r.cause } : {}),
      server_timestamp: now,
    };
  }

  private crewList() {
    const crew: { pid?: string; name: string; stake: number; out: number; win: number; lost: boolean }[] = [];
    for (const b of this.bets.values()) {
      if (b.state === "pending") continue;
      crew.push({ pid: b.pid, name: `${b.name} · ${b.panel}`, stake: b.amount, out: b.multiplier ?? 0, win: b.payout ?? 0, lost: b.state === "lost" });
    }
    for (const bot of this.bots) crew.push({ name: bot.name, stake: bot.stake, out: bot.out, win: bot.win, lost: bot.lost });
    return crew.slice(0, 150);
  }

  /* ---------- internals ---------- */

  private settleCashout(bet: Bet, m: number, mode: "auto" | "manual"): Promise<Msg> {
    // state flips synchronously, so a bet can never be settled twice
    bet.state = "cashed";
    bet.multiplier = m;
    bet.payout = cents(Math.min(bet.amount * m, this.cfg.maxPayout));
    this.crewDirty = true;
    const roundId = this.round.id;
    this.broadcast({ action: "cashout_event", pid: bet.pid, name: bet.name, multiplier: m, payout: bet.payout });
    return this.walletOp("credit", { player_id: bet.pid, round_id: roundId, amount: bet.payout, multiplier: m, panel: "PANEL_" + bet.panel }).then(
      (res) => {
        const ack: Msg = {
          status: "SUCCESS",
          action: "cashout_ack",
          panel: "PANEL_" + bet.panel,
          round_id: roundId,
          multiplier_executed: m,
          payout_amount: bet.payout,
          server_timestamp: this.clock(),
          mode,
          balance: res.balance,
        };
        if (mode === "auto") this.direct(bet.pid, ack);
        return ack;
      },
      (e) => {
        this.walletFailure(bet.pid, e);
        throw this.asGameError(e);
      },
    );
  }

  private async walletOp(kind: "debit" | "credit" | "rollback", req: WalletRequest) {
    const res = await this.wallet[kind](req);
    this.direct(req.player_id, { action: "wallet_tx", endpoint: res.endpoint, payload: res.payload, balance: res.balance });
    return res;
  }

  private walletFailure(pid: string, e: unknown): void {
    // A real deployment would queue the credit for retry with the same transaction_id.
    this.emit("wallet_error", pid, e);
    this.direct(pid, { action: "error", code: "WALLET_ERROR", message: "The wallet did not confirm a payout. It will be retried." });
  }

  private asGameError(e: unknown): GameError {
    if (e instanceof GameError) return e;
    if (e instanceof InsufficientFundsError) return new GameError("INSUFFICIENT_FUNDS", e.message);
    return new GameError("WALLET_ERROR", "The wallet is not responding. Try again.");
  }

  private validStake(amount: number, max: number): number {
    if (typeof amount !== "number" || !Number.isFinite(amount)) throw new GameError("BAD_AMOUNT", "Stake must be a number.");
    const v = cents(amount);
    if (v < this.cfg.tableMin) throw new GameError("BAD_AMOUNT", `Minimum stake is €${this.cfg.tableMin.toFixed(2)}.`);
    if (v > max) throw new GameError("BAD_AMOUNT", `Maximum stake is €${max}.`);
    return v;
  }

  private validAuto(auto: number | null): number | null {
    if (auto === null || auto === undefined) return null;
    if (typeof auto !== "number" || !Number.isFinite(auto)) throw new GameError("BAD_AUTO", "Auto cash-out must be a number.");
    return Math.min(this.cfg.maxMultiplier, Math.max(1.01, floor2(auto)));
  }

  private nameOf(pid: string): string {
    return this.names.get(pid) ?? "Diver-" + pid.slice(-4);
  }

  private spawnBots(): void {
    const rnd = this.random;
    const n = this.cfg.botsMin + Math.floor(rnd() * (this.cfg.botsMax - this.cfg.botsMin + 1));
    const names = [...BOT_NAMES].sort(() => rnd() - 0.5);
    const stakes = [1, 2, 5, 5, 10, 10, 20, 25, 50, 100, 200, 500];
    this.bots = [];
    for (let i = 0; i < n; i++) {
      const r = rnd();
      const target = r < 0.5 ? 1.2 + rnd() * 1.3 : r < 0.85 ? 2.5 + rnd() * 10 : r < 0.97 ? 15 + rnd() * 85 : 100 + rnd() * 900;
      this.bots.push({ name: names[i % names.length]! + (i >= names.length ? "_" + i : ""), stake: stakes[Math.floor(rnd() * stakes.length)]!, target: floor2(target), out: 0, win: 0, lost: false });
    }
  }

  private startRain(now: number): void {
    const amount = [1, 2, 2, 5][Math.floor(this.random() * 4)]!;
    const count = 10 + Math.floor(this.random() * 15);
    this.rain = { id: "rain_" + this.round.nonce, amount, remaining: count, expiresAt: now + RAIN_MS, caught: new Set() };
    this.broadcast({ action: "sonar_rain", id: this.rain.id, amount, count, expires_in_ms: RAIN_MS });
  }

  private botActivity(now: number): void {
    const r = this.rain;
    if (r && now < r.expiresAt && r.remaining > 3 && this.random() < 0.012) {
      r.remaining--;
      this.broadcast({ action: "chat", system: true, text: `${BOT_NAMES[Math.floor(this.random() * BOT_NAMES.length)]} caught a €${r.amount.toFixed(2)} free bet` });
    }
    if ((this.phase === "betting" || this.phase === "descent") && now >= this.nextBotChat) {
      this.nextBotChat = now + 2500 + this.random() * 4500;
      const lines = BOT_LINES[this.phase];
      this.broadcast({ action: "chat", name: BOT_NAMES[Math.floor(this.random() * BOT_NAMES.length)], text: lines[Math.floor(this.random() * lines.length)] });
      if (this.random() < 0.5) this.broadcast({ action: "reaction", emoji: REACTIONS[Math.floor(this.random() * REACTIONS.length)] });
    }
  }

  private broadcast(msg: Msg): void {
    this.emit("broadcast", msg);
  }

  private direct(pid: string, msg: Msg): void {
    this.emit("direct", pid, msg);
  }
}
