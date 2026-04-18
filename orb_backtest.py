"""
Opening Range Breakout backtest.

Records the first 30 minutes of the RTH session, then takes the first
breakout of that range. Stop sits at the opposite end of the range,
target is a fixed R multiple. Forced flat by session close.

Data: Yahoo Finance intraday bars.
Engine: backtrader.
"""

import argparse
import datetime

import backtrader as bt
import backtrader.analyzers as btanalyzers
import pandas as pd
import yfinance as yf


# yfinance limits intraday history by interval
YF_PERIOD_BY_INTERVAL = {
    "1m": "5d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "1h": "730d",
}


def fetch_data(ticker, interval="5m", session_start="09:30", session_end="15:59"):
    """Download OHLCV and return a backtrader PandasData feed."""
    period = YF_PERIOD_BY_INTERVAL.get(interval, "60d")
    print(f"Downloading {ticker} {interval} bars (period={period})")

    df = yf.download(ticker, period=period, interval=interval,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise SystemExit(f"No data returned for {ticker} at {interval}")

    # Strip the multi-level column index yfinance sometimes returns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("US/Eastern").tz_localize(None)

    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    df = df.between_time(session_start, session_end)

    n_days = df.index.normalize().nunique()
    print(f"Loaded {len(df):,} bars across {n_days} trading days "
          f"({df.index[0].date()} to {df.index[-1].date()})\n")

    return bt.feeds.PandasData(dataname=df)


class ORBStrategy(bt.Strategy):
    params = (
        ("tp_rr",          2.0),       # target = tp_rr * range
        ("min_range_pct",  0.05),      # skip days where range is tiny
        ("max_range_pct",  2.0),       # skip gap days with huge ranges
        ("risk_per_trade", 0.01),      # 1% of equity per trade
        ("range_start",    datetime.time(9, 30)),
        ("range_end",      datetime.time(10, 0)),
        ("entry_cutoff",   datetime.time(14, 0)),
        ("session_close",  datetime.time(15, 30)),
    )

    def __init__(self):
        self.entry_order = None
        self.stop_order = None
        self.close_order = None
        self.direction = None
        self.target_price = None

        self.or_high = None
        self.or_low = None
        self.or_locked = False
        self.traded_today = False
        self.current_date = None

    def _cancel_stop(self):
        if self.stop_order:
            self.cancel(self.stop_order)
            self.stop_order = None

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return

        if order == self.entry_order:
            if order.status == order.Completed:
                fill = order.executed.price
                size = abs(order.executed.size)
                if self.direction == "long":
                    stop_px = self.or_low
                    self.target_price = fill + (fill - stop_px) * self.p.tp_rr
                    self.stop_order = self.sell(size=size, exectype=bt.Order.Stop,
                                                price=stop_px)
                else:
                    stop_px = self.or_high
                    self.target_price = fill - (stop_px - fill) * self.p.tp_rr
                    self.stop_order = self.buy(size=size, exectype=bt.Order.Stop,
                                               price=stop_px)
            self.entry_order = None
            return

        if order == self.stop_order:
            if order.status != order.Accepted:
                self.stop_order = None
                self.direction = None
                self.target_price = None
            return

        if order == self.close_order:
            if order.status != order.Accepted:
                self.close_order = None
                self.direction = None
                self.target_price = None

    def next(self):
        bar_date = self.data.datetime.date()
        bar_time = self.data.datetime.time()
        close = self.data.close[0]
        high = self.data.high[0]
        low = self.data.low[0]

        # New day reset
        if bar_date != self.current_date:
            self.current_date = bar_date
            self.or_high = None
            self.or_low = None
            self.or_locked = False
            self.traded_today = False

        # Build opening range
        if self.p.range_start <= bar_time < self.p.range_end and not self.or_locked:
            self.or_high = high if self.or_high is None else max(self.or_high, high)
            self.or_low = low if self.or_low is None else min(self.or_low, low)
            return

        # Lock the range once the window closes
        if not self.or_locked and bar_time >= self.p.range_end:
            self.or_locked = True
            if self.or_high is not None and self.or_low is not None:
                range_pct = (self.or_high - self.or_low) / self.or_low * 100
                if range_pct < self.p.min_range_pct or range_pct > self.p.max_range_pct:
                    # Range was out of bounds, skip the day
                    self.or_high = None
                    self.or_low = None

        in_position = self.position.size != 0
        is_long = self.position.size > 0

        # Force flat at session close
        if in_position and bar_time >= self.p.session_close:
            if not self.close_order:
                self._cancel_stop()
                self.close_order = self.close()
            return

        if self.entry_order or self.close_order:
            return

        # Manage open trade
        if in_position:
            if self.target_price is not None:
                hit_target = (is_long and close >= self.target_price) or \
                             (not is_long and close <= self.target_price)
                if hit_target:
                    self._cancel_stop()
                    self.close_order = self.close()
            return

        # Entry filters
        if self.traded_today:
            return
        if bar_time < self.p.range_end or bar_time >= self.p.entry_cutoff:
            return
        if self.or_high is None or self.or_low is None:
            return

        range_size = self.or_high - self.or_low
        equity = self.broker.getvalue()
        risk_amount = equity * self.p.risk_per_trade
        size = int(risk_amount / range_size)
        if size < 1:
            return

        # Cap size to available cash
        size = min(size, int(equity / close))
        if size < 1:
            return

        if close > self.or_high:
            self.direction = "long"
            self.traded_today = True
            self.entry_order = self.buy(size=size)
        elif close < self.or_low:
            self.direction = "short"
            self.traded_today = True
            self.entry_order = self.sell(size=size)


def print_report(cerebro, results, starting_cash, ticker, interval):
    strat = results[0]
    ta = strat.analyzers.trade_analyzer.get_analysis()
    dd = strat.analyzers.drawdown.get_analysis()

    final_value = cerebro.broker.getvalue()
    total = ta.get("total", {}).get("closed", 0)
    won = ta.get("won", {}).get("total", 0)
    lost = ta.get("lost", {}).get("total", 0)
    win_rate = (won / total * 100) if total else 0.0
    avg_won = ta.get("won", {}).get("pnl", {}).get("average", 0.0)
    avg_lost = ta.get("lost", {}).get("pnl", {}).get("average", 0.0)
    max_dd = dd.get("max", {}).get("drawdown", 0.0)

    net_pnl = final_value - starting_cash
    net_pct = net_pnl / starting_cash * 100
    rr = abs(avg_won / avg_lost) if avg_lost else 0.0
    ev = (win_rate / 100 * avg_won) + ((1 - win_rate / 100) * avg_lost) if total else 0.0

    line = "=" * 52
    print(line)
    print(f"  ORB backtest: {ticker} ({interval} bars)")
    print(line)
    print(f"  Starting equity         ${starting_cash:>12,.2f}")
    print(f"  Final equity            ${final_value:>12,.2f}")
    print(f"  Net P&L                 ${net_pnl:>+12,.2f}  ({net_pct:+.2f}%)")
    print(f"  Max drawdown            {max_dd:>12.2f}%")
    print(line)
    print(f"  Total trades            {total:>12}")
    print(f"  Winners / Losers        {won:>5} / {lost}")
    print(f"  Win rate                {win_rate:>11.1f}%")
    print(f"  Avg winner              ${avg_won:>+12.2f}")
    print(f"  Avg loser               ${avg_lost:>+12.2f}")
    print(f"  Realised R:R            {rr:>12.2f}x")
    print(f"  Expected value / trade  ${ev:>+12.2f}")
    print(line)


def run(ticker, interval, cash, risk, commission):
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=commission)

    cerebro.adddata(fetch_data(ticker, interval=interval))
    cerebro.addstrategy(ORBStrategy, risk_per_trade=risk)
    cerebro.addanalyzer(btanalyzers.TradeAnalyzer, _name="trade_analyzer")
    cerebro.addanalyzer(btanalyzers.DrawDown, _name="drawdown")

    results = cerebro.run()
    print_report(cerebro, results, cash, ticker, interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ORB backtest")
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--interval", default="5m",
                        choices=list(YF_PERIOD_BY_INTERVAL.keys()))
    parser.add_argument("--cash", type=float, default=25_000)
    parser.add_argument("--risk", type=float, default=0.01,
                        help="Fraction of equity to risk per trade")
    parser.add_argument("--commission", type=float, default=0.0)
    args = parser.parse_args()

    run(args.ticker, args.interval, args.cash, args.risk, args.commission)
