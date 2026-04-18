# Opening Range Breakout

A vectorised backtester and a live paper-trading bot for the Opening
Range Breakout (ORB) strategy on US equities.

The idea behind ORB is that the first 30 minutes of the regular session
carry the bulk of institutional order flow. The high and low of that
window set the battle lines for the day. A break of either side is
treated as a directional signal, with the opposite end of the range as
the stop.

## Files

| File | What it does |
| --- | --- |
| `orb_backtest.py` | Downloads intraday bars from Yahoo Finance and backtests ORB on backtrader. |
| `orb_bot_live.py` | Runs the same strategy live against Alpaca paper trading. |
| `requirements.txt` | Python dependencies. |
| `.env.example`    | Template for Alpaca credentials. |

## Strategy rules

1. Record the high and low of 09:30 – 10:00 ET (the opening range).
2. Skip the day if the range is too tight or abnormally wide.
3. First close above the range ⇒ long. First close below ⇒ short.
4. Stop at the opposite end of the range.
5. Target at `R × range` where R is configurable (default 2).
6. No new entries after 14:00 ET. Force flat by 15:30 ET.
7. One trade per day, maximum.

## Installation

```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running the backtest

```
python orb_backtest.py --ticker SPY --interval 5m
python orb_backtest.py --ticker QQQ --interval 15m --risk 0.02
python orb_backtest.py --ticker AAPL --cash 10000
```

Yahoo limits how far back intraday history goes (5 days for 1-minute
bars, 60 days for 5-minute, up to 2 years for 1-hour).

Example output (SPY, 5-minute bars, 60-day window ending 2026-04-17):

```
Downloading SPY 5m bars (period=60d)
Loaded 4,680 bars across 60 trading days (2026-01-22 to 2026-04-17)

====================================================
  ORB backtest: SPY (5m bars)
====================================================
  Starting equity         $   25,000.00
  Final equity            $   26,588.54
  Net P&L                 $   +1,588.54  (+6.35%)
  Max drawdown                    2.64%
====================================================
  Total trades                      60
  Winners / Losers              35 / 25
  Win rate                        58.3%
  Avg winner              $     +109.27
  Avg loser               $      -89.43
  Realised R:R                    1.22x
  Expected value / trade  $      +26.48
====================================================
```

## Running the live bot

Copy `.env.example` to `.env` and fill in your Alpaca paper keys:

```
cp .env.example .env
```

Then:

```
python orb_bot_live.py --ticker SPY
```

The bot writes to `orb_bot.log` as well as stdout. It only trades once
per day, respects a 2.5% daily drawdown cut-off, and flattens any open
position before 15:45 ET.

## Notes and caveats

- This is paper-trading only. Do not point it at a live account.
- Yahoo's intraday feed is fine for research but is not tick-accurate.
  Backtest results are indicative, not exact.
- Slippage and spread are not modelled. Commissions default to zero.
  Set `--commission` to approximate your broker's fees.
- The breakout trigger in the backtester uses a 5-minute close above
  the range. On faster timeframes this reduces whipsaws but slightly
  delays entry versus an intra-bar stop order.

## License

MIT.
