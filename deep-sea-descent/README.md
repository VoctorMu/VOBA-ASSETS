# Deep Sea Descent — playable prototype

Single-file HTML5 prototype of the *Deep Sea Descent* live crash game. Play money only.

Open `index.html` in a browser, or use the published Artifact link.

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

## Not production
In this prototype the RNG, seeds and other players are simulated in the browser. A real deployment needs the server-side engine, a certified RNG, licensing, and responsible-gambling controls described in the spec.
