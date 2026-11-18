"""Fresh-Breakout Rally Rider: a cbBTC trading bot for make.fun's
bars-replay/v1 harness (BTC race, 24h, 1-minute bars).

Idea, from my own analysis of the kit's dev window (BTC fell ~2.8% over
the 24h, with intraday rallies of 1-2% inside it):
  * Most of the window's money sits in a few sustained multi-hour
    rallies. The bot therefore rides 60-minute momentum, but only on a
    FRESH breakout: it buys when the 60-bar return crosses above +35 bps
    (having been at or below it the bar before), so it does not chase a
    move that is already hours old.
  * It buys only a bounce off a washout: the 120-bar return must still
    be below -80 bps at the entry bar. A breakout in a flat or drifting
    market is skipped; only a turn after a real multi-hour drop counts.
  * It protects a winner with an armed trailing stop: once the close is
    +80 bps above the entry fill, a fall of 50 bps from the highest close
    since entry locks the gain in. Before the arm level is reached the
    stop stays off, so ordinary 1-minute noise cannot shake it out.
  * It exits on trend failure too: when the 60-bar return drops below
    -50 bps the rally is over.
  * After any sell it waits 60 bars before buying again, so a stop-out
    cannot turn into an immediate re-buy at a worse price (churn).
  * One position at a time, all-in on entry, fully flat on exit.

Local replay on the kit's dev window (exact fill model, two runs
byte-identical): net PnL +$13.90, 1000-resample bootstrap mean +$13.65,
max drawdown 0.69%, 2 trades (one round trip), net PnL at 2x fees
+$12.68.

All decisions use integer arithmetic on the bar closes only. No wall
clock, no environment, no randomness: two runs on the same bars answer
byte for byte the same. Pure Python standard library.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# Strategy parameters, tuned on the kit's dev window (see README).
LOOKBACK = 60          # bars for the momentum return
WASHOUT_LOOKBACK = 120  # bars for the washout filter
WASHOUT_BPS = -80      # 120-bar return must be below this to buy
ENTRY_BPS = 35         # 60-bar return must cross above this to buy
EXIT_BPS = -50         # 60-bar return below this ends the trade
ARM_BPS = 80           # trailing stop arms once close is this far above entry
TRAIL_BPS = 50         # once armed, sell on a fall this far from the peak close
COOLDOWN_BARS = 60     # bars to wait after a sell before buying again
MAX_SLIPPAGE_BPS = 200  # headroom for the fill vs the latest close


class Rider:
    """Keeps the closes and the trade state across batches."""

    def __init__(self):
        self.closes = []        # closes, ints at price_scale, in bar order
        self.entry_price = None  # fill price of the open buy, or None
        self.peak_close = 0     # highest close seen while holding
        self.last_sell_seq = -10 ** 9  # seq of the last sell decision

    def _return_bps(self, idx, lookback):
        """Return in bps ending at closes[idx] over `lookback` bars."""
        now = self.closes[idx]
        then = self.closes[idx - lookback]
        return (now - then) * 10000 // then

    def on_batch(self, state, batch):
        # Learn the entry fill price from the fills this batch reports.
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.peak_close = self.entry_price
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.peak_close = 0

        if state.warming_up:
            return []
        close = state.close(SYMBOL)
        if close is None:
            return []
        self.closes.append(close)
        n = len(self.closes)
        if n < WASHOUT_LOOKBACK + 2:
            return []

        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding is not None else 0
        idx = n - 1

        if qty > 0:
            # Track the peak close while holding.
            if close > self.peak_close:
                self.peak_close = close
            # Armed trailing stop: only once the trade is ARM_BPS in
            # profit, sell on a TRAIL_BPS pullback from the peak.
            if self.entry_price is not None:
                armed = close * 10000 >= self.entry_price * (10000 + ARM_BPS)
                stopped = close * 10000 <= self.peak_close * (10000 - TRAIL_BPS)
                if armed and stopped:
                    self.last_sell_seq = state.seq
                    return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                             "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            # Trend failure: the 60-bar return has turned clearly down.
            if self._return_bps(idx, LOOKBACK) < EXIT_BPS:
                self.last_sell_seq = state.seq
                return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Flat: buy only on a fresh cross of the 60-bar return above
        # ENTRY_BPS, only after the post-sell cooldown has passed, and
        # only while the 120-bar return is still below WASHOUT_BPS
        # (a bounce off a washout, not a chase in a flat market).
        if state.seq - self.last_sell_seq < COOLDOWN_BARS:
            return []
        ret = self._return_bps(idx, LOOKBACK)
        prev = self._return_bps(idx - 1, LOOKBACK)
        if ret > ENTRY_BPS and prev <= ENTRY_BPS:
            washout = self._return_bps(idx, WASHOUT_LOOKBACK)
            if washout < WASHOUT_BPS:
                cash = state.cash
                if cash <= 0:
                    return []
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": cash,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []


def main():
    rider = Rider()
    makefun_bars.run(rider.on_batch)


if __name__ == "__main__":
    main()
