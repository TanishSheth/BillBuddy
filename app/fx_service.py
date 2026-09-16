"""
Currency Exchange Service for ShareSquad
Provides real-time exchange rates with local TTL caching and settlement conversion math.
"""
import time
import logging
import urllib.request
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

SUPPORTED_CURRENCIES = [
    "USD", "INR", "EUR", "GBP", "CAD", "AUD", "JPY", "AED", "SGD", "CHF", "CNY", "NZD", "THB", "MXN", "BRL"
]

# In-memory cache for exchange rates: { base_currency: { "timestamp": float, "rates": { target: rate } } }
_RATES_CACHE: Dict[str, dict] = {}
CACHE_TTL_SECONDS = 3600  # 1 hour

FALLBACK_USD_RATES = {
    "USD": 1.0,
    "INR": 83.50,
    "EUR": 0.92,
    "GBP": 0.79,
    "CAD": 1.36,
    "AUD": 1.52,
    "JPY": 155.0,
    "AED": 3.67,
    "SGD": 1.35,
    "CHF": 0.91,
    "CNY": 7.24,
    "NZD": 1.64,
    "THB": 36.5,
    "MXN": 16.8,
    "BRL": 5.15,
}


def get_exchange_rates(base: str = "USD") -> Dict[str, float]:
    base = base.upper()
    now = time.time()

    if base in _RATES_CACHE:
        cached = _RATES_CACHE[base]
        if now - cached["timestamp"] < CACHE_TTL_SECONDS:
            return cached["rates"]

    # Try fetching from open.er-api.com (free, reliable, no API key required)
    try:
        url = f"https://open.er-api.com/v6/latest/{base}"
        req = urllib.request.Request(url, headers={"User-Agent": "ShareSquad/1.0"})
        with urllib.request.urlopen(req, timeout=4) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("result") == "success" and "rates" in data:
                    rates = {k: float(v) for k, v in data["rates"].items() if k in SUPPORTED_CURRENCIES}
                    rates[base] = 1.0
                    _RATES_CACHE[base] = {"timestamp": now, "rates": rates}
                    return rates
    except Exception as e:
        logger.warning(f"Could not fetch live exchange rates for {base}: {e}")

    # Fallback derivation using fallback USD table
    if base == "USD":
        rates = FALLBACK_USD_RATES.copy()
    else:
        usd_to_base = FALLBACK_USD_RATES.get(base, 1.0)
        rates = {k: round(v / usd_to_base, 6) for k, v in FALLBACK_USD_RATES.items()}
        rates[base] = 1.0

    _RATES_CACHE[base] = {"timestamp": now, "rates": rates}
    return rates


def get_conversion_rate(from_curr: str, to_curr: str) -> float:
    from_curr = from_curr.upper()
    to_curr = to_curr.upper()
    if from_curr == to_curr:
        return 1.0

    rates = get_exchange_rates(from_curr)
    if to_curr in rates:
        return rates[to_curr]

    # Invert or bridge via USD if missing
    usd_rates = get_exchange_rates("USD")
    from_rate = usd_rates.get(from_curr, 1.0)
    to_rate = usd_rates.get(to_curr, 1.0)
    return round(to_rate / from_rate, 6) if from_rate else 1.0


def convert_amount(amount: Decimal | float, from_curr: str, to_curr: str, custom_rate: Optional[float] = None) -> Tuple[Decimal, Decimal]:
    """
    Returns (converted_amount, rate_used)
    """
    amt = Decimal(str(amount))
    if from_curr.upper() == to_curr.upper():
        return amt.quantize(Decimal("0.01")), Decimal("1.0")

    rate = Decimal(str(custom_rate)) if custom_rate and custom_rate > 0 else Decimal(str(get_conversion_rate(from_curr, to_curr)))
    converted = (amt * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return converted, rate
