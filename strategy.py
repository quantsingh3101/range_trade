"""
Logic for buying and selling.

Take a reference price, and take a buy position there.
From that point:
- If price goes 1000 points UP → sell that position (target hit)
- If price goes 1000 points DOWN → open a new buy position
Each position tracks its own entry price and target independently.

Gap handling:
  If price drops more than target_points in a single candle (e.g. reference=10000,
  current=7000 with target=1000), one position is opened per skipped level, ALL
  sharing the same actual buy price (7000), each with its own distinct target:
    Position 1: buy=7000, target=8000
    Position 2: buy=7000, target=9000
    Position 3: buy=7000, target=10000
  P&L for each = exit_price - 7000 (actual fill price).

Reference point rules:
  - On any buy  → reference_point = buy price (moves down with market)
  - On any sell → reference_point = max(reference_point, exit_price)
                  (moves up when market recovers, so new dips trigger fresh buys)
  This prevents reference_point from getting stranded at a historical low
  while the market has moved significantly higher.

Margin / leverage:
  self.margin = 0.3  →  30% of entry price deployed as collateral per position.
  P&L is on the FULL contract: capital += (exit_price - entry_price) on each sell.
  New positions are only opened when free capital covers entry_price * self.margin.
"""

import pandas as pd
import csv
import os
from datetime import datetime
from data_feed import get_kite_object, update_ohlc_df

kite = None


class Strategy():
    def __init__(self, symbol: str, data: pd.DataFrame, target_points: int, initial_capital: int = 10000000, open_positions_csv: str = None, closed_positions_csv: str = None):
        self.symbol          = symbol
        self.data            = data
        self.req_data        = None
        self.target_points   = target_points
        self.margin          = 0.2   # 20% of entry price as collateral per position
        self.reference_point = 0     # last buy price; rises with sells, falls with buys
        self.open_positions_csv = open_positions_csv  # Path to CSV file for tracking positions
        self.closed_positions_csv = closed_positions_csv  # Path to CSV file for closed trades

        # positions: unique_key -> dict(entry_price, target, entry_time)
        self.positions = {}

        # Capital & metrics
        self.initial_capital = initial_capital
        self.capital         = initial_capital
        self.peak_capital    = initial_capital
        self.max_drawdown    = 0.0
        self.max_drawdown_time = None
        self.num_buys        = 0
        self.num_sells       = 0
        self.num_skipped     = 0
        self.max_simultaneous_positions = 0
        self.max_simultaneous_positions_time = None
        self.trade_log       = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _free_capital(self) -> float:
        """Capital available after deducting margin locked in open positions."""
        margin_in_use = sum(p['entry_price'] * self.margin for p in self.positions.values())
        return self.capital - margin_in_use

    def _can_afford(self, entry_price: float) -> bool:
        return self._free_capital() >= entry_price * self.margin

    def _open_buy(self, current_time, entry_price: float, target: float):
        """Open a single buy position with an explicit target."""
        key = f"{current_time}_{target}"
        self.positions[key] = {
            'entry_price': entry_price,
            'target':      target,
            'entry_time':  current_time,
        }
        charges = 0.005 * entry_price
        self.capital -= charges

        self.num_buys += 1
        print(f"\n[BUY  #{self.num_buys}] Time: {current_time} | "
              f"Entry: {entry_price:.2f} | Target: {target:.2f} | "
              f"Open Positions: {len(self.positions)} | "
              f"Free Capital: {self._free_capital():.2f}")
        
        # Update the open_positions CSV when a new position is opened
        self._update_open_positions_csv()

    def _update_open_positions_csv(self):
        """Update the open_positions CSV file with current positions."""
        if self.open_positions_csv is None:
            return
        
        if not self.positions:
            # Clear the file if no positions
            with open(self.open_positions_csv, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['Entry Time', 'Entry', 'Target', 'Current', 'Unreal.'])
                writer.writeheader()
        else:
            # Write current positions to CSV
            current_price = self.data['close'].iloc[-1]
            rows = []
            for pos in self.positions.values():
                unreal = current_price - pos['entry_price']
                rows.append({
                    'Entry Time': pos['entry_time'],
                    'Entry': pos['entry_price'],
                    'Target': pos['target'],
                    'Current': current_price,
                    'Unreal.': unreal,
                })
            
            df = pd.DataFrame(rows)
            df.to_csv(self.open_positions_csv, index=False)

    def _append_closed_position_csv(self, trade_data: dict):
        """Append a closed trade to the closed_positions CSV file."""
        if self.closed_positions_csv is None:
            return
        
        # Determine next trade_no
        trade_no = 1
        if os.path.exists(self.closed_positions_csv):
            try:
                df_existing = pd.read_csv(self.closed_positions_csv)
                if len(df_existing) > 0:
                    trade_no = int(df_existing['trade_no'].max()) + 1
            except:
                pass
        
        # Prepare row to append
        row = {
            'trade_no': trade_no,
            'buy_date': trade_data['entry_time'],
            'buy_price': round(trade_data['entry_price'], 2),
            'target': round(trade_data['target'], 2),
            'sell_date': trade_data['exit_time'],
            'sell_price': round(trade_data['exit_price'], 2),
            'profit': round(trade_data['profit_points'], 2),
            'profit_pct': round(trade_data['profit_pct'], 2),
            'balance': round(trade_data['balance'], 2),
        }
        
        # Read existing data or create new DataFrame
        if os.path.exists(self.closed_positions_csv):
            df_existing = pd.read_csv(self.closed_positions_csv)
            df_new = pd.DataFrame([row])
            df = pd.concat([df_existing, df_new], ignore_index=True)
        else:
            df = pd.DataFrame([row])
        
        # Write to CSV
        df.to_csv(self.closed_positions_csv, index=False)

    # ------------------------------------------------------------------
    # Core logic
    # ------------------------------------------------------------------

    def take_position(self):
        current_time  = self.req_data['date'].iloc[-1]
        current_price = self.req_data['close'].iloc[-1]

        # --- SELL: check every open position against its own target ---
        for key, pos in list(self.positions.items()):
            if current_price >= pos['target']:
                profit_points = current_price - pos['entry_price']
                profit_pct    = (profit_points / pos['entry_price']) * 100
                charges = 0.005 * current_price  # 0.5% charge on exit price
                self.capital += profit_points - charges  # full contract P&L minus charges
                self.num_sells += 1

                self.trade_log.append({
                    'entry_time':    pos['entry_time'],
                    'exit_time':     current_time,
                    'entry_price':   pos['entry_price'],
                    'target':        pos['target'],
                    'exit_price':    current_price,
                    'profit_points': profit_points,
                    'profit_pct':    profit_pct,
                    'balance':       self.capital,
                })

                del self.positions[key]
                print(f"\n[SELL #{self.num_sells}] Time: {current_time} | "
                      f"Entry: {pos['entry_price']:.2f} | Target: {pos['target']:.2f} | "
                      f"Exit: {current_price:.2f} | "
                      f"Profit: {profit_points:.2f} pts ({profit_pct:.2f}%) | "
                      f"Balance: {self.capital:.2f}")

                # After a sell, anchor reference upward so future dips from
                # this new high trigger fresh buys
                self.reference_point = max(self.reference_point, current_price)
                
                # Append closed position to closed_positions CSV
                self._append_closed_position_csv(self.trade_log[-1])
                
                # Update the open_positions CSV to remove this position
                self._update_open_positions_csv()

        # --- FIRST BUY EVER ---
        if not self.positions and self.reference_point == 0:
            if self._can_afford(current_price):
                self.reference_point = current_price
                self._open_buy(current_time, current_price,
                               current_price + self.target_points)
            else:
                self.num_skipped += 1
            # Update metrics after potential position change
            self._update_metrics(current_time, current_price)
            return

        # --- RESTART: all positions just closed ---
        if not self.positions:
            # Re-anchor to current price and open a fresh position
            self.reference_point = current_price
            if self._can_afford(current_price):
                self._open_buy(current_time, current_price,
                               current_price + self.target_points)
            else:
                self.num_skipped += 1
            # Update metrics after potential position change
            self._update_metrics(current_time, current_price)
            return

        # --- BUY ON DROP: one position per skipped 1000pt level ---
        if current_price <= self.reference_point - self.target_points:
            num_levels = int((self.reference_point - current_price) // self.target_points)

            for i in range(1, num_levels + 1):
                # targets ascend from just above current price up to reference_point
                # e.g. ref=10000, cur=7000 → targets: 8000, 9000, 10000
                target = current_price + i * self.target_points

                if self._can_afford(current_price):
                    self._open_buy(current_time, current_price, target)
                else:
                    self.num_skipped += 1
                    print(f"\n[SKIP] Time: {current_time} | "
                          f"Target {target:.2f} skipped — insufficient free capital "
                          f"(need {current_price * self.margin:.2f}, "
                          f"have {self._free_capital():.2f})")

            # Move reference down to current price (the new lowest buy)
            self.reference_point = current_price

        # Update metrics after all position changes
        self._update_metrics(current_time, current_price)

    def _update_metrics(self, current_time, current_price: float):
        """Update peak capital, max drawdown, and max simultaneous positions."""
        # Track max simultaneous positions
        if len(self.positions) > self.max_simultaneous_positions:
            self.max_simultaneous_positions = len(self.positions)
            self.max_simultaneous_positions_time = current_time
        
        # Calculate total capital including unrealized P&L
        unrealized_pnl = sum(current_price - p['entry_price'] for p in self.positions.values())
        total_capital = self.capital + unrealized_pnl
        
        # Update peak capital
        if total_capital > self.peak_capital:
            self.peak_capital = total_capital
        
        # Calculate max drawdown
        drawdown = (self.peak_capital - total_capital) / self.peak_capital if self.peak_capital > 0 else 0
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown
            self.max_drawdown_time = current_time

    # ------------------------------------------------------------------
    # Summary & export
    # ------------------------------------------------------------------

    def print_summary(self):
        realized_pnl       = sum(t['profit_points'] for t in self.trade_log)
        realized_pnl_pct   = (realized_pnl / self.initial_capital) * 100
        current_price      = self.data['close'].iloc[-1]
        unrealized_pnl     = sum(current_price - p['entry_price'] for p in self.positions.values())
        unrealized_pnl_pct = (unrealized_pnl / self.initial_capital) * 100
        total_capital      = self.capital + unrealized_pnl
        overall_profit     = realized_pnl + unrealized_pnl
        overall_profit_pct = (overall_profit / self.initial_capital) * 100
        current_drawdown   = (self.peak_capital - total_capital) / self.peak_capital if self.peak_capital > 0 else 0
        # current_margin_in_use = sum(p['entry_price'] * self.margin for p in self.positions.values())

        print("\n" + "="*72)
        print("                       SUMMARY")
        print("="*72)
        print(f"  Symbol             : {self.symbol}")
        print(f"  Initial Capital    : {self.initial_capital:.2f}")
        print(f"  Final Capital      : {self.capital:.2f}")
        print(f"  Peak Capital       : {self.peak_capital:.2f}")
        # print(f"  Margin in Use      : {current_margin_in_use:.2f}")
        print(f"  Overall Profit     : {overall_profit:.2f} ({overall_profit_pct:.2f}%)")
        print(f"  Realized PnL       : {realized_pnl:.2f} ({realized_pnl_pct:.2f}%)")
        print(f"  Unrealized PnL     : {unrealized_pnl:.2f} ({unrealized_pnl_pct:.2f}%)")
        print(f"  Total Capital      : {total_capital:.2f}")
        print(f"  Max Drawdown       : {self.max_drawdown*100:.2f}% @ {self.max_drawdown_time}")
        print(f"  Current Drawdown   : {current_drawdown*100:.2f}%")
        print(f"  Max Simultaneous   : {self.max_simultaneous_positions} positions @ {self.max_simultaneous_positions_time}")
        print(f"  Total Buys         : {self.num_buys}")
        print(f"  Total Sells        : {self.num_sells}")
        print(f"  Skipped (no margin): {self.num_skipped}")
        print(f"  Completed Trades   : {len(self.trade_log)}")
        print("-"*72)

        # if self.trade_log:
            # print(f"  {'#':<4} {'Entry Time':<22} {'Exit Time':<22} "
            #       f"{'Entry':>8} {'Target':>8} {'Exit':>8} {'Profit':>8} {'Balance':>12}")
            # print("-"*72)
            # for i, t in enumerate(self.trade_log, 1):
            #     print(f"  {i:<4} {str(t['entry_time']):<22} {str(t['exit_time']):<22} "
            #           f"{t['entry_price']:>8.0f} {t['target']:>8.0f} {t['exit_price']:>8.0f} "
            #           f"{t['profit_points']:>8.0f} {t['balance']:>12.2f}")

        if self.positions:
            print(f"\n  Open Positions     : {len(self.positions)}")
            print(f"  {'Entry Time':<22} {'Entry':>10} {'Target':>10} {'Current':>10} {'Unreal.':>10}")
            print("  " + "-"*65)
            for key, pos in self.positions.items():
                unreal = current_price - pos['entry_price']
                print(f"  {str(pos['entry_time']):<22} {pos['entry_price']:>10.2f} "
                      f"{pos['target']:>10.2f} {current_price:>10.2f} {unreal:>+10.2f}")

        print("="*72)

    def _export_trades_csv(self):
        """Export closed trade log to CSV with running balance column."""
        if not self.trade_log:
            print("  No completed trades to export.")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename  = f"trades_{self.symbol}_{timestamp}.csv"

        fieldnames = [
            "trade_no",
            "buy_date",
            "buy_price",
            "target",
            "sell_date",
            "sell_price",
            "profit",
            "profit_pct",
            "balance",
        ]

        with open(filename, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for i, t in enumerate(self.trade_log, 1):
                writer.writerow({
                    "trade_no":  i,
                    "buy_date":  t["entry_time"],
                    "buy_price": round(t["entry_price"], 4),
                    "target":    round(t["target"], 4),
                    "sell_date": t["exit_time"],
                    "sell_price":round(t["exit_price"], 4),
                    "profit":    round(t["profit_points"], 4),
                    "profit_pct":round(t["profit_pct"], 4),
                    "balance":   round(t["balance"], 4),
                })

        print(f"\n  ✓ Trade log exported → {os.path.abspath(filename)}")

    def run(self):
        for i in range(len(self.data)):
            self.req_data = self.data.iloc[:i+1]
            self.take_position()

        self.print_summary()
        self._export_trades_csv()


if __name__ == "__main__":
    import time
    symbol = 'SILVERMIC26JUNFUT'
    data = pd.read_csv(f'{symbol}_minute.csv')
    # data = data.loc['2026-06-08 9:00':]
    data = pd.read_csv(f'{symbol}_minute.csv', parse_dates=['date'])
    data = data[data['date'] >= '2026-06-08 9:00']
    print(data.head())
    time.sleep(5)
    target_points = 1000
    strat = Strategy(symbol=symbol, data=data, target_points=target_points)
    print(f"\n{'='*72}")
    print(f"  Starting Backtest | Symbol: {strat.symbol} | Target: {strat.target_points} pts")
    print(f"  Capital: {strat.initial_capital} | Margin: {strat.margin*100:.0f}%")
    print(f"{'='*72}")
    strat.run()