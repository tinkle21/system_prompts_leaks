"""
Wheel Strategy Trader — Alpaca Paper Trading
============================================
Implements the options wheel strategy:
  Stage 1: Sell cash-secured puts
  Stage 2: If assigned, sell covered calls
  Repeats until told to stop.

Run with:
    python wheel_strategy.py --stock AAPL
"""

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from config import (
    CALL_STRIKE_PCT_ABOVE,
    CHECK_INTERVAL_MIN,
    EXPIRY_MAX_DAYS,
    EXPIRY_MIN_DAYS,
    MARKET_CLOSE_HOUR,
    MARKET_CLOSE_MINUTE,
    MARKET_OPEN_HOUR,
    MARKET_OPEN_MINUTE,
    PROFIT_CLOSE_PCT,
    PUT_STRIKE_PCT_BELOW,
    load_credentials,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class WheelState:
    stock: str
    stage: str = "PUTS"          # "PUTS" or "CALLS"
    cost_basis: float = 0.0      # average cost per share (incl. premiums received)
    shares_owned: int = 0
    total_premium: float = 0.0   # cumulative premium collected
    open_contract: dict = field(default_factory=dict)  # active option order info

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self.__dict__, f, indent=2)

    @classmethod
    def load(cls, stock: str):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                data = json.load(f)
            if data.get("stock") == stock:
                obj = cls(**data)
                return obj
        return cls(stock=stock)


# ---------------------------------------------------------------------------
# Alpaca REST helper
# ---------------------------------------------------------------------------

class AlpacaClient:
    def __init__(self):
        creds = load_credentials()
        self.base = creds["base_url"]
        self.headers = {
            "APCA-API-KEY-ID": creds["api_key"],
            "APCA-API-SECRET-KEY": creds["secret_key"],
            "Content-Type": "application/json",
        }

    def _get(self, path, **params):
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path, payload):
        r = requests.post(f"{self.base}{path}", headers=self.headers, json=payload)
        r.raise_for_status()
        return r.json()

    def _delete(self, path):
        r = requests.delete(f"{self.base}{path}", headers=self.headers)
        r.raise_for_status()

    # --- account ---
    def get_account(self):
        return self._get("/account")

    def buying_power(self) -> float:
        return float(self.get_account()["buying_power"])

    def portfolio_value(self) -> float:
        return float(self.get_account()["portfolio_value"])

    # --- market data ---
    def latest_price(self, symbol: str) -> float:
        data = self._get(f"/stocks/{symbol}/quotes/latest")
        ask = float(data["quote"]["ap"])
        bid = float(data["quote"]["bp"])
        return round((ask + bid) / 2, 2)

    # --- positions ---
    def get_position(self, symbol: str):
        try:
            return self._get(f"/positions/{symbol}")
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    # --- options chain ---
    def get_options_chain(self, symbol: str, option_type: str, target_expiry: date):
        """Fetch option contracts near target_expiry for symbol."""
        expiry_str = target_expiry.isoformat()
        data = self._get(
            "/options/contracts",
            underlying_symbols=symbol,
            type=option_type,
            expiration_date_gte=(date.today() + timedelta(days=EXPIRY_MIN_DAYS)).isoformat(),
            expiration_date_lte=(date.today() + timedelta(days=EXPIRY_MAX_DAYS)).isoformat(),
            status="active",
            limit=100,
        )
        return data.get("option_contracts", [])

    def get_option_quote(self, symbol: str) -> dict:
        """Get latest quote for an option contract symbol."""
        try:
            data = self._get(f"/options/contracts/{symbol}/quotes/latest")
            return data.get("quote", {})
        except Exception:
            return {}

    # --- orders ---
    def submit_option_order(self, symbol: str, qty: int, side: str, limit_price: float) -> dict:
        payload = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(round(limit_price, 2)),
        }
        return self._post("/orders", payload)

    def get_order(self, order_id: str) -> dict:
        return self._get(f"/orders/{order_id}")

    def cancel_order(self, order_id: str):
        self._delete(f"/orders/{order_id}")

    def close_position(self, symbol: str):
        self._delete(f"/positions/{symbol}")


# ---------------------------------------------------------------------------
# Strategy logic
# ---------------------------------------------------------------------------

class WheelTrader:
    def __init__(self, stock: str):
        self.client = AlpacaClient()
        self.state = WheelState.load(stock)
        log.info("Loaded state: stage=%s  shares=%d  premium=%.2f",
                 self.state.stage, self.state.shares_owned, self.state.total_premium)

    # --- helpers ---

    def _pick_expiry(self) -> date:
        """Pick an expiration 2-4 weeks out (target ~3 weeks)."""
        target = date.today() + timedelta(days=21)
        return target

    def _select_put_contract(self, contracts: list, current_price: float):
        """Pick the put contract with strike closest to 10% below current price."""
        target_strike = round(current_price * (1 - PUT_STRIKE_PCT_BELOW), 2)
        candidates = [
            c for c in contracts
            if c.get("type") == "put" and float(c["strike_price"]) <= current_price
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(float(c["strike_price"]) - target_strike))

    def _select_call_contract(self, contracts: list, cost_basis: float):
        """Pick the call contract with strike closest to 10% above cost basis, never below it."""
        target_strike = round(cost_basis * (1 + CALL_STRIKE_PCT_ABOVE), 2)
        candidates = [
            c for c in contracts
            if c.get("type") == "call" and float(c["strike_price"]) >= cost_basis
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(float(c["strike_price"]) - target_strike))

    def _contract_profit_pct(self, contract_symbol: str, original_credit: float) -> float:
        """Return profit as a fraction of original credit (0.5 = 50% profit)."""
        if original_credit <= 0:
            return 0.0
        quote = self.client.get_option_quote(contract_symbol)
        mark = (float(quote.get("ap", 0)) + float(quote.get("bp", 0))) / 2
        if mark <= 0:
            return 0.0
        profit_pct = (original_credit - mark) / original_credit
        return profit_pct

    # --- stage 1: sell cash-secured put ---

    def run_puts_stage(self):
        price = self.client.latest_price(self.state.stock)
        bp = self.client.buying_power()
        log.info("PUTS stage | %s price=%.2f  buying_power=%.2f", self.state.stock, price, bp)

        # Check we have enough cash to buy 100 shares if assigned
        cash_needed = price * 100 * (1 - PUT_STRIKE_PCT_BELOW)
        if bp < cash_needed:
            log.warning("Not enough buying power (%.2f) to sell a cash-secured put (need %.2f). Skipping.", bp, cash_needed)
            return

        contracts = self.client.get_options_chain(self.state.stock, "put", self._pick_expiry())
        if not contracts:
            log.warning("No put contracts found. Will retry next cycle.")
            return

        contract = self._select_put_contract(contracts, price)
        if not contract:
            log.warning("Could not select a suitable put contract.")
            return

        symbol = contract["symbol"]
        strike = float(contract["strike_price"])
        expiry = contract["expiration_date"]
        quote = self.client.get_option_quote(symbol)
        bid = float(quote.get("bp", 0))

        if bid <= 0:
            log.warning("Put %s has no bid. Skipping.", symbol)
            return

        # Sell 1 put contract (= 100 shares)
        log.info("Selling put: %s  strike=%.2f  expiry=%s  credit=%.2f", symbol, strike, expiry, bid)
        order = self.client.submit_option_order(symbol, qty=1, side="sell", limit_price=bid)

        premium = bid * 100
        self.state.total_premium += premium
        self.state.open_contract = {
            "order_id": order["id"],
            "symbol": symbol,
            "type": "put",
            "strike": strike,
            "expiry": expiry,
            "original_credit": bid,
        }
        self.state.save()
        log.info("Put order submitted. Premium collected: %.2f  Total: %.2f", premium, self.state.total_premium)

    # --- stage 2: sell covered call ---

    def run_calls_stage(self):
        price = self.client.latest_price(self.state.stock)
        log.info("CALLS stage | %s price=%.2f  cost_basis=%.2f  shares=%d",
                 self.state.stock, price, self.state.cost_basis, self.state.shares_owned)

        contracts = self.client.get_options_chain(self.state.stock, "call", self._pick_expiry())
        if not contracts:
            log.warning("No call contracts found. Will retry next cycle.")
            return

        contract = self._select_call_contract(contracts, self.state.cost_basis)
        if not contract:
            log.warning("No suitable call above cost basis %.2f found.", self.state.cost_basis)
            return

        symbol = contract["symbol"]
        strike = float(contract["strike_price"])
        expiry = contract["expiration_date"]
        quote = self.client.get_option_quote(symbol)
        bid = float(quote.get("bp", 0))

        if bid <= 0:
            log.warning("Call %s has no bid. Skipping.", symbol)
            return

        log.info("Selling call: %s  strike=%.2f  expiry=%s  credit=%.2f", symbol, strike, expiry, bid)
        order = self.client.submit_option_order(symbol, qty=1, side="sell", limit_price=bid)

        premium = bid * 100
        self.state.total_premium += premium
        self.state.open_contract = {
            "order_id": order["id"],
            "symbol": symbol,
            "type": "call",
            "strike": strike,
            "expiry": expiry,
            "original_credit": bid,
        }
        self.state.save()
        log.info("Call order submitted. Premium collected: %.2f  Total: %.2f", premium, self.state.total_premium)

    # --- monitor open contract ---

    def monitor_open_contract(self):
        if not self.state.open_contract:
            return

        c = self.state.open_contract
        symbol = c["symbol"]
        original_credit = c.get("original_credit", 0)

        # Check for early 50% profit close
        profit_pct = self._contract_profit_pct(symbol, original_credit)
        log.info("Contract %s profit: %.1f%%", symbol, profit_pct * 100)

        if profit_pct >= PROFIT_CLOSE_PCT:
            log.info("50%% profit target hit on %s — closing early.", symbol)
            quote = self.client.get_option_quote(symbol)
            ask = float(quote.get("ap", 0))
            self.client.submit_option_order(symbol, qty=1, side="buy", limit_price=ask)
            log.info("Buy-to-close order submitted for %s", symbol)
            self.state.open_contract = {}
            self.state.save()
            return

        # Check if contract expired / was assigned
        expiry = date.fromisoformat(c["expiry"])
        if date.today() > expiry:
            log.info("Contract %s has expired. Checking assignment status.", symbol)
            self._handle_expiry(c)

    def _handle_expiry(self, contract: dict):
        position = self.client.get_position(self.state.stock)

        if contract["type"] == "put":
            if position and int(position["qty"]) >= 100:
                # Assigned — now own shares
                avg_price = float(position["avg_entry_price"])
                self.state.stage = "CALLS"
                self.state.shares_owned = int(position["qty"])
                # Cost basis = avg entry price minus premium per share received so far
                premiums_per_share = self.state.total_premium / self.state.shares_owned
                self.state.cost_basis = avg_price - premiums_per_share
                log.info("PUT ASSIGNED. Shares: %d  Avg entry: %.2f  Cost basis: %.2f",
                         self.state.shares_owned, avg_price, self.state.cost_basis)
            else:
                log.info("Put expired worthless. Back to selling another put.")

        elif contract["type"] == "call":
            if not position or int(position.get("qty", 0)) < 100:
                # Shares called away
                self.state.stage = "PUTS"
                self.state.shares_owned = 0
                self.state.cost_basis = 0.0
                log.info("CALL ASSIGNED. Shares sold. Returning to PUTS stage.")
            else:
                log.info("Call expired worthless. Selling another covered call.")

        self.state.open_contract = {}
        self.state.save()

    # --- daily summary ---

    def daily_summary(self):
        account = self.client.get_account()
        portfolio_value = float(account["portfolio_value"])
        equity = float(account["equity"])
        position = self.client.get_position(self.state.stock)

        log.info("=" * 60)
        log.info("DAILY SUMMARY — %s", datetime.now(ET).strftime("%Y-%m-%d"))
        log.info("Stock          : %s", self.state.stock)
        log.info("Current Stage  : %s", self.state.stage)
        log.info("Shares Owned   : %d", self.state.shares_owned)
        log.info("Cost Basis     : $%.2f/share", self.state.cost_basis)
        log.info("Total Premium  : $%.2f", self.state.total_premium)
        log.info("Portfolio Value: $%.2f", portfolio_value)
        log.info("Equity         : $%.2f", equity)
        if position:
            unrealized_pl = float(position.get("unrealized_pl", 0))
            log.info("Unrealized P&L : $%.2f", unrealized_pl)
        log.info("Total Return   : $%.2f", self.state.total_premium)
        log.info("=" * 60)

    # --- main cycle ---

    def run_cycle(self):
        """One check cycle: monitor open contract, or open a new position."""
        if self.state.open_contract:
            self.monitor_open_contract()
            # If contract was closed/expired, open_contract is now empty
            if self.state.open_contract:
                return  # Still have an open contract, nothing more to do

        # No open contract — open a new one
        if self.state.stage == "PUTS":
            self.run_puts_stage()
        elif self.state.stage == "CALLS":
            self.run_calls_stage()


# ---------------------------------------------------------------------------
# Market hours check
# ---------------------------------------------------------------------------

def is_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    open_time = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    return open_time <= now < close_time


def is_market_close() -> bool:
    """True for the 5-minute window right at/after market close."""
    now = datetime.now(ET)
    close_time = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    window = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE + 5, second=0, microsecond=0)
    return close_time <= now < window


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Wheel Strategy Trader via Alpaca Paper Trading")
    parser.add_argument("--stock", required=True, help="Ticker symbol, e.g. AAPL")
    args = parser.parse_args()

    stock = args.stock.upper()
    log.info("Starting Wheel Strategy for %s", stock)

    trader = WheelTrader(stock)
    daily_summary_done = False

    while True:
        now = datetime.now(ET)

        if is_market_open():
            daily_summary_done = False
            log.info("Market is open. Running cycle...")
            try:
                trader.run_cycle()
            except Exception as e:
                log.error("Cycle error: %s", e, exc_info=True)
            log.info("Sleeping %d minutes until next check.", CHECK_INTERVAL_MIN)
            time.sleep(CHECK_INTERVAL_MIN * 60)

        elif is_market_close() and not daily_summary_done:
            trader.daily_summary()
            daily_summary_done = True
            time.sleep(60)

        else:
            # Outside market hours — do nothing, check every 5 minutes
            log.debug("Market closed. Waiting... (%s ET)", now.strftime("%H:%M"))
            time.sleep(5 * 60)


if __name__ == "__main__":
    main()
