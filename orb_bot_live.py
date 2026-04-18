"""
Live ORB bot for Alpaca paper trading.

Runs during the regular US session. Builds the 9:30-10:00 ET opening
range, then waits for the first breakout. Stops sit at the opposite
end of the range; targets are a fixed R multiple. Forced flat before
the close.

Defaults to paper trading. Configure credentials in a .env file:

    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...
    ALPACA_PAPER=true
"""

import argparse
import datetime
import logging
import os
import sys
import time

import pytz
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest


load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
IS_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

# Strategy settings
TP_RR = 2.0
MIN_RANGE_PCT = 0.05
MAX_RANGE_PCT = 2.0
DAILY_LOSS_PCT = 0.025      # hard stop for the day
POLL_SECONDS = 15

# Session times (US/Eastern)
EST = pytz.timezone("US/Eastern")
RANGE_START = datetime.time(9, 30)
RANGE_END = datetime.time(10, 0)
ENTRY_CUTOFF = datetime.time(14, 0)
SESSION_CLOSE = datetime.time(15, 30)
EOD_FLAT = datetime.time(15, 45)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("orb_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("orb")


def now_est():
    return datetime.datetime.now(EST)


def latest_price(data_client, ticker):
    req = StockLatestTradeRequest(symbol_or_symbols=ticker)
    resp = data_client.get_stock_latest_trade(req)
    return float(resp[ticker].price)


def open_position(trading, ticker):
    try:
        return trading.get_open_position(ticker)
    except Exception:
        return None


def submit_order(trading, ticker, qty, side):
    req = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    return trading.submit_order(req)


def flatten(trading, ticker):
    try:
        trading.close_position(ticker)
    except Exception as exc:
        log.warning(f"close_position failed: {exc}")


def run(ticker, risk_pct):
    trading = TradingClient(API_KEY, SECRET_KEY, paper=IS_PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    account = trading.get_account()
    equity = float(account.equity)
    loss_limit = -equity * DAILY_LOSS_PCT
    log.info(f"ORB bot starting | {ticker} | equity=${equity:,.2f} | paper={IS_PAPER}")

    or_high = None
    or_low = None
    or_locked = False

    trade = None           # {'dir','entry','stop','tp','shares'}
    traded_today = False
    daily_pnl = 0.0

    while True:
        t = now_est().time()

        # End of day: flatten and exit
        if t >= EOD_FLAT:
            if trade:
                flatten(trading, ticker)
                log.info(f"EOD flat | daily P&L (est): ${daily_pnl:,.2f}")
            log.info("Session finished, shutting down")
            return

        # Daily loss circuit breaker
        if daily_pnl <= loss_limit:
            if trade:
                flatten(trading, ticker)
            log.warning(f"Daily loss limit hit (${daily_pnl:,.2f}). Stopping.")
            return

        # Sleep until the session opens
        if t < RANGE_START:
            time.sleep(10)
            continue

        # Build opening range
        if RANGE_START <= t < RANGE_END and not or_locked:
            try:
                price = latest_price(data_client, ticker)
                or_high = price if or_high is None else max(or_high, price)
                or_low = price if or_low is None else min(or_low, price)
            except Exception as exc:
                log.warning(f"price fetch failed while building range: {exc}")
            time.sleep(POLL_SECONDS)
            continue

        # Lock the range once the window closes
        if not or_locked and t >= RANGE_END:
            or_locked = True
            if or_high is None or or_low is None:
                log.warning("No range captured, skipping today")
            else:
                range_pct = (or_high - or_low) / or_low * 100
                if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
                    log.info(f"Range {range_pct:.2f}% out of bounds, skipping today")
                    or_high = or_low = None
                else:
                    log.info(f"OR locked: [{or_low:.2f} - {or_high:.2f}] "
                             f"({range_pct:.2f}%)")

        # Manage an active position
        if trade:
            pos = open_position(trading, ticker)
            if pos is None:
                log.info("Position closed externally")
                trade = None
            else:
                px = float(pos.current_price)
                unrealised = float(pos.unrealized_pl)

                hit_stop = (trade["dir"] == "LONG" and px <= trade["stop"]) or \
                           (trade["dir"] == "SHORT" and px >= trade["stop"])
                hit_tp = (trade["dir"] == "LONG" and px >= trade["tp"]) or \
                         (trade["dir"] == "SHORT" and px <= trade["tp"])

                if hit_stop or hit_tp or t >= SESSION_CLOSE:
                    reason = "stop" if hit_stop else "target" if hit_tp else "session close"
                    flatten(trading, ticker)
                    daily_pnl += unrealised
                    log.info(f"Exit {trade['dir']} via {reason} @ ${px:.2f} "
                             f"| trade P&L=${unrealised:+.2f} | day=${daily_pnl:+.2f}")
                    trade = None

        # Look for a breakout entry
        if (not trade and not traded_today and or_locked and or_high is not None
                and t < ENTRY_CUTOFF):
            try:
                price = latest_price(data_client, ticker)
            except Exception as exc:
                log.warning(f"price fetch failed for entry: {exc}")
                time.sleep(POLL_SECONDS)
                continue

            effective_equity = equity + daily_pnl
            range_size = or_high - or_low
            shares = int(effective_equity * risk_pct / range_size)
            shares = min(shares, int(effective_equity / price))

            if shares >= 1:
                if price > or_high:
                    entry = price
                    stop = or_low
                    tp = entry + (entry - stop) * TP_RR
                    submit_order(trading, ticker, shares, "BUY")
                    trade = {"dir": "LONG", "entry": entry, "stop": stop,
                             "tp": tp, "shares": shares}
                    traded_today = True
                    log.info(f"LONG {shares} {ticker} @ ${entry:.2f} "
                             f"| stop ${stop:.2f} | tp ${tp:.2f}")

                elif price < or_low:
                    entry = price
                    stop = or_high
                    tp = entry - (stop - entry) * TP_RR
                    submit_order(trading, ticker, shares, "SELL")
                    trade = {"dir": "SHORT", "entry": entry, "stop": stop,
                             "tp": tp, "shares": shares}
                    traded_today = True
                    log.info(f"SHORT {shares} {ticker} @ ${entry:.2f} "
                             f"| stop ${stop:.2f} | tp ${tp:.2f}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ORB live bot (Alpaca paper)")
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--risk", type=float, default=0.01,
                        help="Fraction of equity to risk per trade")
    args = parser.parse_args()

    if not API_KEY or not SECRET_KEY:
        log.error("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. Check your .env.")
        sys.exit(1)

    run(args.ticker, args.risk)
