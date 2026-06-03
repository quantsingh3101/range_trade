import pickle
import pandas as pd
from kiteconnect import KiteTicker
import threading
import yfinance as yf
from datetime import datetime, timedelta
import time





def get_kite_object():
    """
    Load authenticated KiteConnect object from pickle
    """
    with open("kite.pickle", "rb") as f:
        kite = pickle.load(f)

    return kite



kite = get_kite_object()




def fetch_and_update(interval='minute'):
    # --- Initial fetch ---
    data = kite.historical_data(
        instrument_token=122157319,
        from_date=datetime(2026, 5, 28),
        to_date=datetime.now(),
        interval=interval
    )
    df = pd.DataFrame(data, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)  # strip timezone
    return df


print('DATA FEED - LIVE')
while True:
    df = fetch_and_update()
    print(df.tail(1))
    print('-' * 75)
    time.sleep(5)
