import { test } from "node:test";
import assert from "node:assert/strict";
import WebSocket from "ws";
import { createGameServer } from "../src/server.js";
import { outcome } from "../src/fair.js";

const FAST = { betMs: 300, launchMs: 100, crashMs: 150, kBase: 2, botsMin: 2, botsMax: 3 };

class Client {
  msgs: any[] = [];
  private waiters: { pred: (m: any) => boolean; resolve: (m: any) => void }[] = [];
  constructor(readonly ws: WebSocket) {
    ws.on("message", (d) => {
      const m = JSON.parse(d.toString());
      this.msgs.push(m);
      this.waiters = this.waiters.filter((w) => (w.pred(m) ? (w.resolve(m), false) : true));
    });
  }
  static async connect(url: string) {
    const ws = new WebSocket(url.replace("http", "ws") + "/ws");
    await new Promise((r, j) => (ws.once("open", r), ws.once("error", j)));
    return new Client(ws);
  }
  send(m: unknown) {
    this.ws.send(JSON.stringify(m));
  }
  next(pred: (m: any) => boolean, ms = 4000): Promise<any> {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("timed out waiting for message")), ms);
      this.waiters.push({ pred, resolve: (m) => (clearTimeout(t), resolve(m)) });
    });
  }
  async request(m: Record<string, unknown>, pred: (r: any) => boolean) {
    const p = this.next(pred);
    this.send(m);
    return p;
  }
}

test("WebSocket protocol: hello, bet, cash-out ACK and broadcasts", async (t) => {
  const app = await createGameServer({ port: 0, host: "127.0.0.1", config: FAST, stepMs: 5, outcomeFn: () => ({ hmac: "", crash: 50, surge: false, cause: "hull" }) });
  t.after(() => app.close());

  const a = await Client.connect(app.url);
  const b = await Client.connect(app.url);
  t.after(() => (a.ws.close(), b.ws.close()));

  const err = await a.request({ action: "bet", panel: "PANEL_A", amount: 1 }, (m) => m.action === "error");
  assert.equal(err.code, "HELLO_REQUIRED");

  const welcome = await a.request({ action: "hello", player_id: "usr_testaaaa", name: "Tester <b>" }, (m) => m.action === "welcome");
  assert.equal(welcome.player_id, "usr_testaaaa");
  assert.equal(welcome.name, "Tester b");
  assert.equal(welcome.balance, 1000);
  assert.equal(welcome.round.client_seed, "000000000000000000042d38e211832049e2cf31");
  await b.request({ action: "hello", name: "Watcher" }, (m) => m.action === "welcome");

  const bad = await a.request({ action: "nope" }, (m) => m.action === "error");
  assert.equal(bad.code, "UNKNOWN_ACTION");

  await a.next((m) => m.action === "round_betting");
  const bet = await a.request({ action: "bet", panel: "PANEL_A", amount: 10, auto_cashout: null }, (m) => m.action === "bet_ack" || m.action === "error");
  assert.equal(bet.action, "bet_ack", JSON.stringify(bet));
  assert.equal(bet.balance, 990);

  const descent = await a.next((m) => m.action === "round_descent");
  assert.equal(descent.round_id, bet.round_id);
  await a.next((m) => m.action === "tick");
  const sent = Date.now();
  const ack = await a.request({ action: "cashout", panel: "PANEL_A", round_id: bet.round_id, client_timestamp: sent }, (m) => m.action === "cashout_ack");
  assert.equal(ack.status, "SUCCESS");
  assert.equal(ack.round_id, bet.round_id);
  assert.ok(ack.multiplier_executed >= 1 && ack.multiplier_executed < 50);
  assert.equal(ack.payout_amount, Math.round(10 * ack.multiplier_executed * 100) / 100);
  assert.ok(Date.now() - sent < 200, "ACK should be fast in-process");

  const seen = await b.next((m) => m.action === "cashout_event" && m.pid === "usr_testaaaa");
  assert.equal(seen.payout, ack.payout_amount);

  const chat = b.next((m) => m.action === "chat" && m.pid === "usr_testaaaa");
  a.send({ action: "chat", text: "  bubbles\u0007 ahoy  " });
  assert.equal((await chat).text, "bubbles ahoy");
});

test("HTTP: page wrapper, health and provably-fair verify", async (t) => {
  const app = await createGameServer({ port: 0, host: "127.0.0.1", config: { ...FAST, kBase: 6 }, stepMs: 5 });
  t.after(() => app.close());

  const page = await fetch(app.url + "/");
  const html = await page.text();
  assert.equal(page.status, 200);
  assert.match(html, /^<!doctype html>/);
  assert.match(html, /window\.DSD_SERVER = "\/ws"/);
  assert.match(html, /<title>Deep Sea Descent<\/title>/);

  const health = await (await fetch(app.url + "/healthz")).json();
  assert.equal(health.ok, true);

  const roundId = app.engine.round.id;
  const early = await fetch(app.url + "/api/v1/provably-fair/verify?round_id=" + roundId);
  assert.equal(early.status, 404, "seed must not be revealed before the implosion");

  const c = await Client.connect(app.url);
  t.after(() => c.ws.close());
  c.send({ action: "hello" });
  const crash = await c.next((m) => m.action === "round_crash", 8000);
  const res = await fetch(app.url + "/api/v1/provably-fair/verify?round_id=" + crash.round_id);
  const body = await res.json();
  assert.equal(res.status, 200);
  for (const k of ["round_id", "server_seed", "server_seed_hash", "client_seed", "nonce", "result_multiplier"]) assert.ok(k in body, k);
  assert.equal(body.result_multiplier, crash.crash);
  assert.equal(outcome(body.server_seed, body.client_seed, body.nonce, app.engine.cfg).crash, body.result_multiplier);
});
