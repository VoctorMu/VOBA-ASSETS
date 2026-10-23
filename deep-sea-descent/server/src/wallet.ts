import { cents } from "./config.js";

/** Operator seamless-wallet calls, shaped like POST /wallet/debit and /wallet/credit in the spec. */
export interface WalletRequest {
  player_id: string;
  round_id: string;
  amount: number;
  /** callers may pass their own id so a retried call is applied once */
  transaction_id?: string;
  [extra: string]: unknown;
}

export interface WalletPayload {
  transaction_id: string;
  player_id: string;
  game_id: string;
  round_id: string;
  currency: string;
  amount: number;
  timestamp: number;
  [extra: string]: unknown;
}

export interface WalletResult {
  endpoint: string;
  payload: WalletPayload;
  balance: number;
}

export interface Wallet {
  balance(playerId: string): Promise<number>;
  debit(req: WalletRequest): Promise<WalletResult>;
  credit(req: WalletRequest): Promise<WalletResult>;
  /** reverses an earlier debit, e.g. a bet cancelled during the betting window */
  rollback(req: WalletRequest): Promise<WalletResult>;
  log(playerId: string): WalletResult[];
}

export class InsufficientFundsError extends Error {
  constructor() {
    super("Not enough balance for that stake.");
  }
}

/** In-memory stand-in for an operator wallet. Play money only. */
export class InMemoryWallet implements Wallet {
  private balances = new Map<string, number>();
  private logs = new Map<string, WalletResult[]>();
  private applied = new Map<string, WalletResult>();
  private seq = 9981247129841;

  constructor(
    private readonly startingBalance: number,
    private readonly currency: string,
    private readonly clock: () => number = Date.now,
  ) {}

  async balance(playerId: string): Promise<number> {
    return this.ensure(playerId);
  }

  debit(req: WalletRequest) {
    return this.apply("debit", req, -1);
  }

  credit(req: WalletRequest) {
    return this.apply("credit", req, 1);
  }

  rollback(req: WalletRequest) {
    return this.apply("rollback", req, 1);
  }

  log(playerId: string): WalletResult[] {
    return this.logs.get(playerId) ?? [];
  }

  private ensure(playerId: string): number {
    if (!this.balances.has(playerId)) this.balances.set(playerId, this.startingBalance);
    return this.balances.get(playerId)!;
  }

  private async apply(kind: string, req: WalletRequest, sign: 1 | -1): Promise<WalletResult> {
    if (req.transaction_id && this.applied.has(req.transaction_id)) return this.applied.get(req.transaction_id)!;
    const amount = cents(req.amount);
    if (!(amount >= 0) || !Number.isFinite(amount)) throw new RangeError("Wallet amount must be a non-negative number.");
    const bal = this.ensure(req.player_id);
    if (sign < 0 && bal < amount) throw new InsufficientFundsError();
    const { transaction_id, player_id, round_id, amount: _a, ...extra } = req;
    const payload: WalletPayload = {
      transaction_id: transaction_id ?? "tx_" + this.seq++,
      player_id,
      game_id: "deep-sea-descent",
      round_id,
      currency: this.currency,
      amount,
      ...extra,
      timestamp: this.clock(),
    };
    const balance = cents(bal + sign * amount);
    this.balances.set(player_id, balance);
    const result: WalletResult = { endpoint: "POST /wallet/" + kind, payload, balance };
    if (transaction_id) {
      this.applied.set(transaction_id, result);
      if (this.applied.size > 10000) this.applied.delete(this.applied.keys().next().value!);
    }
    const log = this.logs.get(player_id) ?? [];
    log.unshift(result);
    if (log.length > 50) log.pop();
    this.logs.set(player_id, log);
    return result;
  }
}
