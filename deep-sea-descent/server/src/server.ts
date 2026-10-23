import http from "node:http";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { randomBytes } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";
import { WebSocketServer, WebSocket, type RawData } from "ws";
import { type GameConfig, type ZoneId } from "./config.js";
import { GameEngine, GameError, type EngineOptions, type PanelId } from "./engine.js";
import { InMemoryWallet, type Wallet } from "./wallet.js";

export interface ServerOptions {
  port?: number;
  host?: string;
  config?: Partial<GameConfig>;
  /** path to the client page fragment (deep-sea-descent/index.html) */
  clientPath?: string;
  stepMs?: number;
  wallet?: Wallet;
  /** test hook: fixed outcomes instead of the provably-fair HMAC */
  outcomeFn?: EngineOptions["outcomeFn"];
}

type Incoming = { action?: unknown; [k: string]: unknown };

const PLAYER_ID = /^usr_[a-z0-9]{6,24}$/;
const MAX_MSGS_PER_SEC = 30;

function findClient(start: string): string {
  let dir = start;
  for (let i = 0; i < 5; i++) {
    const candidate = path.join(dir, "index.html");
    if (existsSync(candidate)) return candidate;
    dir = path.dirname(dir);
  }
  throw new Error("Could not find the client index.html. Pass clientPath.");
}

function parsePanel(v: unknown): PanelId {
  if (v === "A" || v === "PANEL_A") return "A";
  if (v === "B" || v === "PANEL_B") return "B";
  throw new GameError("BAD_PANEL", "Panel must be PANEL_A or PANEL_B.");
}

function cleanName(v: unknown, pid: string): string {
  const s = typeof v === "string" ? v.replace(/[^\w .-]/g, "").trim().slice(0, 20) : "";
  return s || "Diver-" + pid.slice(-4);
}

export async function createGameServer(opts: ServerOptions = {}) {
  const config = opts.config ?? {};
  const wallet = opts.wallet ?? new InMemoryWallet(config.startingBalance ?? 1000, config.currency ?? "EUR");
  const engine = new GameEngine({ wallet, config, outcomeFn: opts.outcomeFn });
  const clientPath = opts.clientPath ?? findClient(path.dirname(fileURLToPath(import.meta.url)));
  const sockets = new Map<string, Set<WebSocket>>();

  const json = (res: http.ServerResponse, status: number, body: unknown) => {
    res.writeHead(status, { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" });
    res.end(JSON.stringify(body));
  };

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url ?? "/", "http://local");
    try {
      if (req.method !== "GET") return json(res, 405, { error: "Method not allowed" });
      if (url.pathname === "/" || url.pathname === "/index.html") {
        // The client file is an artifact fragment; wrap it in a document and point it at this server.
        const fragment = await readFile(clientPath, "utf8");
        res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-content-type-options": "nosniff" });
        return res.end(
          `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><script>window.DSD_SERVER = "/ws";</script></head><body>${fragment}</body></html>`,
        );
      }
      if (url.pathname === "/healthz") return json(res, 200, { ok: true, round_id: engine.round.id, phase: engine.phase, players_online: sockets.size });
      if (url.pathname === "/api/v1/rounds/current") return json(res, 200, engine.publicRound());
      if (url.pathname === "/api/v1/provably-fair/verify") {
        const record = engine.verify(url.searchParams.get("round_id") ?? "");
        if (!record) return json(res, 404, { error: "Unknown round, or the round has not finished yet. Seeds are revealed after the implosion." });
        const { surge, ...payload } = record;
        return json(res, 200, { ...payload, surge });
      }
      if (url.pathname === "/api/v1/wallet/transactions") {
        // demo inspection endpoint for the mock wallet; a real operator wallet would not be exposed like this
        const pid = url.searchParams.get("player_id") ?? "";
        if (!PLAYER_ID.test(pid)) return json(res, 400, { error: "player_id is required." });
        return json(res, 200, { player_id: pid, balance: await wallet.balance(pid), transactions: wallet.log(pid) });
      }
      return json(res, 404, { error: "Not found" });
    } catch (e) {
      return json(res, 500, { error: "Server error" });
    }
  });

  const wss = new WebSocketServer({ server, path: "/ws", maxPayload: 4096 });

  const sendRaw = (ws: WebSocket, data: string) => {
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  };
  const send = (ws: WebSocket, msg: unknown) => sendRaw(ws, JSON.stringify(msg));

  engine.on("broadcast", (msg) => {
    const data = JSON.stringify(msg);
    for (const set of sockets.values()) for (const ws of set) sendRaw(ws, data);
  });
  engine.on("direct", (pid: string, msg) => {
    const set = sockets.get(pid);
    if (!set) return;
    const data = JSON.stringify(msg);
    for (const ws of set) sendRaw(ws, data);
  });
  engine.on("wallet_error", (pid: string, e: unknown) => console.error(`[wallet] ${pid}:`, e));

  async function handle(pid: string, m: Incoming) {
    switch (m.action) {
      case "bet":
        return engine.placeBet(pid, parsePanel(m.panel), m.amount as number, (m.auto_cashout ?? null) as number | null);
      case "cancel":
        return engine.cancelBet(pid, parsePanel(m.panel));
      case "set_auto":
        return engine.setAuto(pid, parsePanel(m.panel), (m.auto_cashout ?? null) as number | null);
      case "cashout":
        return engine.cashout(pid, parsePanel(m.panel), typeof m.client_timestamp === "number" ? m.client_timestamp : undefined);
      case "side_bet":
        return engine.sideBet(pid, m.zone as ZoneId, m.amount as number);
      case "side_clear":
        return engine.sideClear(pid);
      case "chat":
        return engine.chat(pid, String(m.text ?? ""));
      case "reaction":
        return engine.reaction(pid, String(m.emoji ?? ""));
      case "rain_catch":
        return engine.catchRain(pid, String(m.id ?? ""));
      case "wheel_spin":
        return engine.wheelSpin(pid);
      case "reset_wallet":
        return engine.resetWallet(pid);
      case "ping":
        return { action: "pong", server_timestamp: Date.now() };
      default:
        throw new GameError("UNKNOWN_ACTION", "Unknown action.");
    }
  }

  wss.on("connection", (ws) => {
    let pid: string | null = null;
    let alive = true;
    let windowStart = Date.now();
    let count = 0;
    ws.on("pong", () => (alive = true));
    const beat = setInterval(() => {
      if (!alive) return ws.terminate();
      alive = false;
      ws.ping();
    }, 30000);

    ws.on("message", async (data: RawData) => {
      const now = Date.now();
      if (now - windowStart > 1000) {
        windowStart = now;
        count = 0;
      }
      if (++count > MAX_MSGS_PER_SEC) return;
      let m: Incoming;
      try {
        m = JSON.parse(data.toString());
        if (!m || typeof m !== "object" || typeof m.action !== "string") throw new Error();
      } catch {
        return send(ws, { action: "error", code: "BAD_MESSAGE", message: "Messages must be JSON objects with an action." });
      }
      if (!pid) {
        if (m.action !== "hello") return send(ws, { action: "error", code: "HELLO_REQUIRED", message: "Send hello first." });
        // Demo identity: a real deployment would take the player from the operator's signed launch token.
        pid = typeof m.player_id === "string" && PLAYER_ID.test(m.player_id) ? m.player_id : "usr_" + randomBytes(6).toString("hex");
        engine.setName(pid, cleanName(m.name, pid));
        if (!sockets.has(pid)) sockets.set(pid, new Set());
        sockets.get(pid)!.add(ws);
        return send(ws, await engine.snapshot(pid));
      }
      try {
        const ack = await handle(pid, m);
        if (ack) send(ws, ack);
      } catch (e) {
        const err = e instanceof GameError ? e : new GameError("SERVER_ERROR", "Something went wrong on the server.");
        if (!(e instanceof GameError)) console.error(e);
        send(ws, { action: "error", code: err.code, message: err.message, ref: m.action, panel: typeof m.panel === "string" ? m.panel : undefined });
      }
    });

    ws.on("close", () => {
      clearInterval(beat);
      if (!pid) return;
      const set = sockets.get(pid);
      set?.delete(ws);
      if (set && !set.size) sockets.delete(pid);
    });
  });

  engine.start();
  const loop = setInterval(() => engine.step(), opts.stepMs ?? 20);

  await new Promise<void>((resolve) => server.listen(opts.port ?? 8080, opts.host ?? "0.0.0.0", resolve));
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : opts.port ?? 8080;

  return {
    engine,
    wallet,
    server,
    port,
    url: `http://localhost:${port}`,
    async close() {
      clearInterval(loop);
      for (const c of wss.clients) c.terminate();
      await new Promise<void>((r) => wss.close(() => r()));
      await new Promise<void>((r) => server.close(() => r()));
    },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const app = await createGameServer({ port: Number(process.env.PORT) || 8080, host: process.env.HOST });
  console.log(`Deep Sea Descent server running at ${app.url} (WebSocket ${app.url.replace("http", "ws")}/ws)`);
  const stop = async () => {
    await app.close();
    process.exit(0);
  };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}
