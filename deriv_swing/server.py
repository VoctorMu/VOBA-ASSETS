"""FastAPI backend for the dashboard. The engine runs in a background thread."""
from __future__ import annotations

import math
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import Engine
from .strategy import Analysis

STATIC = Path(__file__).parent / "static"


def analysis_json(an: Analysis, bars: int = 300) -> dict:
    c = an.candles
    if c is None or c.empty:
        return {"symbol": an.symbol, "status": an.status, "candles": []}
    off = max(0, len(c) - bars)
    t = c["time"].to_numpy()

    def tp(i, price):
        return {"time": int(t[i]), "price": float(price)}

    candles = [{"time": int(r.time), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
               for r in c.iloc[off:].itertuples()]
    rsi = []
    if an.rsi is not None:
        for i, v in enumerate(an.rsi.to_numpy()[off:], start=off):
            if not math.isnan(v):
                rsi.append({"time": int(t[i]), "value": round(float(v), 2)})
    sig = an.signal
    return {
        "symbol": an.symbol,
        "bar_time": an.bar_time,
        "status": an.status,
        "bias_d1": an.bias_d1,
        "bias_w1": an.bias_w1,
        "atr": an.atr,
        "candles": candles,
        "rsi": rsi,
        "swings": [{"time": int(t[s.idx]), "price": s.price, "kind": s.kind} for s in an.swings if s.idx >= off],
        "patterns": [{
            "label": p.label, "name": p.name, "direction": p.direction,
            "points": [tp(i, v) for i, v in p.points if i >= off],
            "neckline": [tp(i, v) for i, v in p.neckline if i >= off],
            "break_time": int(t[p.break_idx]), "stop": p.stop,
        } for p in an.patterns],
        "divergences": [{
            "label": d.label, "direction": d.direction,
            "p1": {**tp(d.p1[0], d.p1[1]), "rsi": d.p1[2]},
            "p2": {**tp(d.p2[0], d.p2[1]), "rsi": d.p2[2]},
        } for d in an.divergences if d.p1[0] >= off],
        "amds": [{
            "direction": a.direction, "start": int(t[a.range_start]), "end": int(t[a.range_end]),
            "high": a.range_high, "low": a.range_low, "manip_time": int(t[a.manip_idx]),
            "manip_extreme": a.manip_extreme, "dist_time": int(t[a.dist_idx]),
        } for a in an.amds if a.range_start >= off],
        "signal": None if sig is None else {
            "direction": sig.direction, "close": sig.close, "stop": sig.stop, "reasons": sig.reasons,
            "setup_id": sig.setup_id,
        },
    }


class Control(BaseModel):
    action: str


def create_app(engine: Engine, cfg: dict, run_engine: bool = True) -> FastAPI:
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        if run_engine:
            threading.Thread(target=engine.run_forever, args=(stop,), daemon=True, name="engine").start()
        yield
        stop.set()

    app = FastAPI(title="Deriv Swing", lifespan=lifespan)
    token = cfg["dashboard"]["token"]

    def auth(request: Request):
        if not token:
            return
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(given, token):
            raise HTTPException(401, "bad token")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        with engine.lock:
            acct = engine.broker.account()
            symbols = []
            for sym in cfg["symbols"]:
                an = engine.analyses.get(sym)
                symbols.append({
                    "symbol": sym,
                    "bias_d1": an.bias_d1 if an else 0,
                    "bias_w1": an.bias_w1 if an else 0,
                    "status": engine.errors.get(sym) or (an.status if an else "waiting for data"),
                    "error": sym in engine.errors,
                    "signal": bool(an and an.signal),
                    "bar_time": an.bar_time if an else None,
                    "last": float(an.candles["close"].iloc[-1]) if an is not None and an.candles is not None else None,
                })
            g = engine.guard
            return {
                "execution": cfg["execution"], "data_source": cfg["data_source"], "paused": engine.paused,
                "now": engine.feed.now(), "last_step": engine.last_step,
                "account": asdict(acct),
                "risk": {
                    "risk_per_trade_pct": cfg["risk"]["risk_per_trade_pct"],
                    "reward_risk": cfg["risk"]["reward_risk"],
                    "daily_cap_pct": g.cap_pct, "day": g.day, "day_start_equity": g.start_equity,
                    "day_loss_pct": g.loss_pct(acct.equity),
                    "breakeven": cfg["risk"].get("breakeven", {}),
                },
                "whatsapp": engine.notifier.enabled,
                "symbols": symbols,
            }

    @app.get("/api/analysis/{symbol}", dependencies=[Depends(auth)])
    def analysis(symbol: str):
        with engine.lock:
            an = engine.analyses.get(symbol)
            if an is None:
                raise HTTPException(404, "no analysis yet")
            return analysis_json(an)

    @app.get("/api/trades", dependencies=[Depends(auth)])
    def trades():
        with engine.lock:
            return {"open": [asdict(p) for p in engine.broker.open_positions()],
                    "closed": [asdict(t) for t in engine.broker.closed_trades(100)]}

    @app.get("/api/signals", dependencies=[Depends(auth)])
    def signals():
        return engine.journal.recent(100)

    @app.post("/api/control", dependencies=[Depends(auth)])
    def control(body: Control):
        if body.action == "pause":
            engine.set_paused(True)
        elif body.action == "resume":
            engine.set_paused(False)
        elif body.action == "close_all":
            engine.set_paused(True)
            return {"closed": engine.close_all(), "paused": True}
        else:
            raise HTTPException(400, "unknown action")
        return {"paused": engine.paused}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
