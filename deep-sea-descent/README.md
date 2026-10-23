# Deep Sea Descent — playable prototype

Single-file HTML5 prototype of the *Deep Sea Descent* live crash game. Play money only.

Two ways to run it:

- **Offline**: open `index.html` or the published Artifact link. Rounds, seeds and other players are simulated in the browser.
- **Server mode**: run the Node game server. It serves the same page and drives it over WebSocket, so every connected player rides the same dive.

```bash
cd server
npm install
npm start            # builds, then serves http://localhost:8080  (PORT=… to change)
npm test             # 19 tests: maths, engine, WebSocket protocol, HTTP API
npm run dev          # watch mode with tsx
```

Open http://localhost:8080 in two tabs to see shared dives, chat and the win feed. If the page can't reach its server, it shows a notice and switches to the offline simulation.

## What's in it
- **Round cycle**: 8 s betting → 2 s launch → descent → 3 s implosion and reset.
- **Dual bet panels**: Panel A (Safety) and Panel B (Deep Dive), each with its own stake and auto cash-out. You can queue a bet for the next dive.
- **Crash maths**: P(reach x) = 0.965 / x, 3.5% instant implosions at 1.00x, a 10,000x cap and a €250,000 payout cap.
- **Provably fair**: `HMAC_SHA256(serverSeed, clientSeed + ":" + nonce)`. The first 52 bits give r, and crash = floor(96.5 / (1 − r)) / 100. The seed hash is shown before each dive and the seed is revealed after it. The Fair tab recomputes and verifies any round.
- **Sonar Radar side bets** and **Pressure Surge** (20% of dives; the climb rate and side-bet payouts double).
- **Mock seamless wallet**: `/wallet/debit`, `/wallet/credit` and `/wallet/rollback` payloads in the spec's shape, logged in the Wallet tab.
- **Social**: simulated crew, live win feed, chat, emoji reaction bubbles, and Sonar Rain free-bet drops.
- **Gamification**: daily missions that unlock hull skins and a trail, the Oxygen Tank daily wheel, and the progressive 500x jackpot drop.

## Side-bet payout correction
The spec's side-bet payouts miss the 95.5% RTP target. Twilight at 3.50x would return about 135%. Payouts were re-solved from `1.2 · p · payout ≤ 0.955`, where the 1.2 factor covers Surge doubling in 1 of 5 dives:

| Zone | P(zone) | Payout | Surge | RTP |
|---|---|---|---|---|
| Sunlight | 51.75% | 1.53x | 3.06x | 95.01% |
| Twilight | 38.60% | 2.06x | 4.12x | 95.42% |
| Midnight | 8.685% | 9.16x | 18.32x | 95.46% |
| Abyssal | 0.965% | 82.40x | 164.80x | 95.42% |

## Game server (`server/`)
Node + TypeScript with the `ws` library and in-memory state. The server decides everything: it generates seeds, times rounds, prices cash-outs and settles bets. The client only renders and sends intents.

| File | Role |
|---|---|
| `src/engine.ts` | Round state machine (betting → launch → descent → crash), bets, auto cash-outs, side bets, jackpot, Sonar Rain, wheel, server bots. Uses an injectable clock so tests can step time exactly. |
| `src/fair.ts` | HMAC-SHA256 crash outcome. A test runs the client's own code to confirm both sides compute identical results. |
| `src/wallet.ts` | `Wallet` interface, which a real operator adapter would implement, plus an in-memory mock with idempotent `transaction_id`s. |
| `src/server.ts` | HTTP routes plus the WebSocket at `/ws`: per-connection rate limit, 4 KB message cap, ping/pong heartbeat. |

**Fairness**: all players share one dive, so the client seed is a single public value (shown in the Fair tab) rather than a per-player seed. The server seed's hash is published when betting opens, and the seed is revealed in `round_crash`.

**Cash-out timing**: the multiplier is `e^(k·t)`. On a cash-out request the server first settles any crash that is already due, then pays `floor2(e^(k·t))` at the moment the request arrives. A request that arrives after the crash point is refused with `TOO_LATE`, even if no tick has run since.

### WebSocket protocol (`ws://host/ws`, JSON)
Client → server: start with `hello {player_id?, name?}`, then:

| action | fields |
|---|---|
| `bet` | `panel` (`PANEL_A`/`PANEL_B`), `amount`, `auto_cashout` (number or null) |
| `cancel`, `set_auto` | `panel` (+ `auto_cashout`) |
| `cashout` | `panel`, `round_id`, `client_timestamp` (spec shape) |
| `side_bet` / `side_clear` | `zone` (`sun`/`twi`/`mid`/`aby`), `amount` |
| `chat` / `reaction` | `text` / `emoji` |
| `rain_catch`, `wheel_spin`, `reset_wallet`, `ping` | – |

Server → client: `welcome` (full snapshot), `round_betting`, `round_launch`, `round_descent`, `tick`, `round_crash`, `bet_ack`, `cancel_ack`, `cashout_ack` (spec shape plus `mode` and `balance`), `bet_lost`, `side_ack`, `side_result`, `wallet_tx`, `crew`, `cashout_event`, `chat`, `reaction`, `sonar_rain`, `rain_ack`, `jackpot_drop`, `jackpot_share`, `wheel_result`, `error {code, message, ref, panel}`.

### HTTP
- `GET /api/v1/provably-fair/verify?round_id=` returns the spec payload. It returns 404 until the round has crashed.
- `GET /api/v1/rounds/current`, `GET /healthz`
- `GET /api/v1/wallet/transactions?player_id=` is a demo-only view of the mock wallet.

### Maths note
With crash points floored to whole cents, **1.00x shows on about 4.46% of dives**, not the spec's 3.50%. The 3.50% are true instant implosions; another ~0.96% land between 1.00x and 1.0099x, which also displays as 1.00x. RTP is still 96.5%, because the lowest cash-out is 1.01x. The spec's "exactly 3.50% terminate at 1.00x" line should say "3.50% implode instantly".

## Not production
Still play money, and the server keeps everything in memory, so a restart wipes wallets. Identity is whatever `player_id` the browser sends; a real deployment would take the player from the operator's signed launch token. Going to production would also need Redis/Postgres persistence, a real wallet adapter with a retry queue for failed credits, a certified hardware RNG, horizontal scaling, video streaming, licensing, and responsible-gambling controls.
