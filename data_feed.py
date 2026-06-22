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


def update_ohlc_df(kite, instrument_token = '122157319', interval='minute'):
    """
    Updates the latest candle with current time and live price.
    Latest candle timestamp is adjusted to datetime.now() and close price from ltp.

    Returns:
        updated df
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(minutes=10)
    df = pd.DataFrame(kite.historical_data(instrument_token, from_date=from_date, to_date=to_date, interval=interval))
    
    # Convert date to datetime and remove timezone
    df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
    
    # Update the latest candle's timestamp to current time
    df.at[len(df) - 1, 'date'] = datetime.now()
    
    # Update the close price with latest live price (no delay)
    latest_price = kite.ltp('MCX:SILVERMIC26JUNFUT')['MCX:SILVERMIC26JUNFUT']['last_price']
    df.at[len(df) - 1, 'close'] = latest_price
    
    df.set_index('date', inplace=True)
    return df


# while True:
#     kite = get_kite_object()
#     df = update_ohlc_df(kite)
#     print(df.tail())
#     time.sleep(10)