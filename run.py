"""Start the engine and dashboard:  python run.py [--config config.yaml]"""
from __future__ import annotations

import argparse
import logging
import shutil

import uvicorn

from deriv_swing.broker import MT5Broker, PaperBroker
from deriv_swing.config import ROOT, load_config
from deriv_swing.data import MT5Feed, SyntheticFeed
from deriv_swing.engine import Engine
from deriv_swing.server import create_app


def connect_mt5(cfg):
    import MetaTrader5 as mt5  # Windows only: pip install MetaTrader5

    m = cfg["mt5"]
    kwargs = {k: m[k] for k in ("login", "password", "server") if m.get(k)}
    ok = mt5.initialize(m["path"], **kwargs) if m.get("path") else mt5.initialize(**kwargs)
    if not ok:
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.account_info()
    logging.info("Connected to MT5 account %s on %s (%s)", info.login, info.server,
                 "DEMO" if info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL")
    return mt5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    state_dir = ROOT / "state" / cfg["execution"]

    if cfg["data_source"] == "mt5":
        mt5 = connect_mt5(cfg)
        feed = MT5Feed(mt5)
    else:
        feed = SyntheticFeed(cfg["symbols"], speed=cfg["engine"]["synthetic_speed"])
        cfg["engine"]["poll_seconds"] = min(cfg["engine"]["poll_seconds"], 2)
        state_dir = ROOT / "state" / "synthetic"
        shutil.rmtree(state_dir, ignore_errors=True)  # synthetic clock restarts, so start fresh

    if cfg["execution"] == "mt5":
        broker = MT5Broker(mt5, cfg["mt5"]["magic"], cfg["mt5"]["deviation_points"])
    else:
        broker = PaperBroker(feed, cfg["risk"]["paper_starting_balance"], state_dir / "paper.json")

    engine = Engine(cfg, feed, broker, state_dir)
    app = create_app(engine, cfg)
    uvicorn.run(app, host=cfg["dashboard"]["host"], port=cfg["dashboard"]["port"], log_level="warning")


if __name__ == "__main__":
    main()
