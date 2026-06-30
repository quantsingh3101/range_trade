# Script for paper trading the range breakout strategy using live feed from Kite Connect
import sys
from datetime import datetime, time, timedelta
import pandas as pd
import time as time_module
from strategy import Strategy
from data_feed import get_kite_object, update_ohlc_df

# Initialize KiteConnect object
kite = get_kite_object()
mcx_instrument_df = pd.read_csv('range_trade/mcx_instrument_df.csv')
token = mcx_instrument_df[mcx_instrument_df['tradingsymbol'] == 'SILVERMIC26JUNFUT']['DB Symbol'].values[0]
open_positions_df = pd.read_csv('range_trade/open_positions.csv')

# Configuration
SYMBOL = 'SILVERMIC26JUNFUT'
TARGET_POINTS = 1000
INITIAL_CAPITAL = 9975517.35
INSTRUMENT_TOKEN = '122157319'
FETCH_INTERVAL = 10 

# Get initial historical data (10 minutes worth)
print(f"\n{'='*72}")
print(f"  Initializing Paper Trading | Symbol: {SYMBOL} | Target: {TARGET_POINTS} pts")
print(f"  Capital: {INITIAL_CAPITAL} | Margin: 30%")
print(f"{'='*72}\n")

print("[INFO] Fetching initial historical data...")
initial_df = update_ohlc_df(kite, instrument_token=INSTRUMENT_TOKEN)
initial_df = initial_df.reset_index()

# Initialize strategy
strategy = Strategy(
    symbol=SYMBOL,
    data=initial_df,
    target_points=TARGET_POINTS,
    initial_capital=INITIAL_CAPITAL,
    open_positions_csv='range_trade/open_positions.csv',
    closed_positions_csv='range_trade/closed_positions.csv'
)

# Load existing open positions from CSV
if len(open_positions_df) > 0:
    print(f"\n[INFO] Loading {len(open_positions_df)} existing positions from open_positions.csv...")
    
    for idx, row in open_positions_df.iterrows():
        entry_time = pd.to_datetime(row['Entry Time']).to_pydatetime()
        entry_price = float(row['Entry'])
        target = float(row['Target'])
        
        # Create position key in the format: {current_time}_{target}
        key = f"{entry_time}_{target}"
        strategy.positions[key] = {
            'entry_price': entry_price,
            'target': target,
            'entry_time': entry_time,
        }
        
        print(f"  [LOADED] Entry: {entry_price:.2f} | Target: {target:.2f} | Time: {entry_time}")
    
    # Set reference_point to the minimum entry price (latest buy level)
    min_entry_price = open_positions_df['Entry'].min()
    strategy.reference_point = min_entry_price
    print(f"\n[INFO] Reference point set to: {strategy.reference_point:.2f}")
    print(f"[INFO] Total open positions loaded: {len(strategy.positions)}\n")
else:
    print("[INFO] No existing positions found. Starting fresh...\n")

last_candle_time = None
consecutive_errors = 0
MAX_CONSECUTIVE_ERRORS = 5

print(f"[INFO] Strategy initialized with {len(initial_df)} candles")
print(f"[INFO] Starting paper trading loop...\n")

while True:
    try:
        # Fetch latest OHLC data
        df = update_ohlc_df(kite, instrument_token=INSTRUMENT_TOKEN)
        df = df.reset_index()
        
        current_time = df['date'].iloc[-1]
        current_price = df['close'].iloc[-1]
        
        # Check if we have a new candle
        if last_candle_time is None or current_time > last_candle_time:
            # Update strategy data with new candle
            strategy.data = df
            strategy.req_data = df.iloc[-1:]
            
            # Execute trading logic
            strategy.take_position()
            
            last_candle_time = current_time
            
            print(f"\n[DATA] Time: {current_time} | Price: {current_price:.2f} | "
                  f"Open Positions: {len(strategy.positions)}")
            strategy.print_summary()

            consecutive_errors = 0
        else:
            print(f"[INFO] Waiting for new candle... (current: {current_time})")
        
        # Sleep before next fetch
        time_module.sleep(FETCH_INTERVAL)
        
    except Exception as e:
        consecutive_errors += 1
        print(f"\n[ERROR] ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}) {str(e)}")
        
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"\n[FATAL] Too many consecutive errors. Exiting...")
            print(f"[INFO] Printing final summary...\n")
            strategy.print_summary()
            sys.exit(1)
        
        time_module.sleep(10)  # Wait 10 seconds before retrying

