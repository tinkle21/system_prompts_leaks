import json
import os

CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")

def load_credentials():
    with open(CREDENTIALS_FILE) as f:
        creds = json.load(f)
    return creds["alpaca"]

# Wheel strategy parameters
PUT_STRIKE_PCT_BELOW   = 0.10   # Sell put 10% below current price
CALL_STRIKE_PCT_ABOVE  = 0.10   # Sell call 10% above cost basis
EXPIRY_MIN_DAYS        = 14     # Minimum DTE (days to expiration)
EXPIRY_MAX_DAYS        = 28     # Maximum DTE
PROFIT_CLOSE_PCT       = 0.50   # Close contract early at 50% profit
CHECK_INTERVAL_MIN     = 15     # Check positions every 15 minutes

# Market hours (Eastern Time)
MARKET_OPEN_HOUR       = 9
MARKET_OPEN_MINUTE     = 30
MARKET_CLOSE_HOUR      = 16
MARKET_CLOSE_MINUTE    = 0
