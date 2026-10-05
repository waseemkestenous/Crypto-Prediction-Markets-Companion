#!/usr/bin/env python3
"""
Crypto Prediction Markets Companion for Robinhood

Multi-asset 15-minute prediction-market companion using a free multi-exchange USD proxy.

DESIGN GOAL
-----------
The prediction is made FROM THE START OF THE ROUND using ONLY information
that existed BEFORE that round started.

It does NOT:
- ask you for a target
- ask you for YES / NO prices
- ask you for number of contracts
- change the original frozen prediction used for scoring

It DOES:
- detect the current :00 / :15 / :30 / :45 round automatically
- estimate/capture the round-start benchmark automatically
- analyze historical BTC behavior before the round
- freeze one UP/DOWN probability at the start
- publish a separate live recommendation once per minute using the current
  price, the move since round start, and the time remaining
- evaluate that frozen prediction at settlement
- print HIT or MISS
- keep cumulative accuracy
- immediately continue to the next 15-minute round

MODEL
-----
The probability is based on:
1. Similar historical 15-minute setups (nearest-neighbor pattern matching)
2. Recent 3-hour 15-minute move distribution
3. Bootstrap simulation using ONLY pre-round one-minute returns
4. A small trend/regime component

The dollar target is NOT an input to the probability model.
It is used only to display/evaluate the round.

BENCHMARK
---------
This version does NOT require a CF Benchmarks license.

It builds a FREE multi-exchange BRTI-like proxy from public BTC/USD data
(Coinbase, Kraken, Bitstamp, Gemini and Crypto.com when available).

This is NOT official CME CF BRTI. Robinhood's official settlement can differ.

INSTALL
-------
pip install requests numpy

RUN
---
python btc.py --asset BTC

Optional:
python btc.py --asset ETH --context-hours 3 --pattern-days 7

Stop with Ctrl+C.
"""

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests


# ============================================================
# CONFIG
# ============================================================

# Free/public exchange endpoints used to construct a BRTI-LIKE proxy.
# This is NOT the official CME CF BRTI.
COINBASE_BASE = "https://api.exchange.coinbase.com"

KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"

ASSET_CONFIGS = {
    "BTC": {"coinbase": "BTC-USD", "bitstamp": "btcusd", "kraken": "XBTUSD", "gemini": "btcusd", "cryptocom": "BTC_USD"},
    "ETH": {"coinbase": "ETH-USD", "bitstamp": "ethusd", "kraken": "ETHUSD", "gemini": "ethusd", "cryptocom": "ETH_USD"},
    "SOL": {"coinbase": "SOL-USD", "bitstamp": "solusd", "kraken": "SOLUSD", "gemini": None, "cryptocom": "SOL_USD"},
    "XRP": {"coinbase": "XRP-USD", "bitstamp": "xrpusd", "kraken": "XRPUSD", "gemini": "xrpusd", "cryptocom": "XRP_USD"},
    "DOGE": {"coinbase": "DOGE-USD", "bitstamp": "dogeusd", "kraken": "XDGUSD", "gemini": "dogeusd", "cryptocom": "DOGE_USD"},
    "BNB": {"coinbase": "BNB-USD", "bitstamp": "bnbusd", "kraken": "BNBUSD", "gemini": "bnbusd", "cryptocom": None},
    "HYPE": {"coinbase": "HYPE-USD", "bitstamp": "hypeusd", "kraken": "HYPEUSD", "gemini": "hypeusd", "cryptocom": "HYPE_USD"},
}
DEFAULT_ASSET = "BTC"

CRYPTOCOM_BASE = "https://api.crypto.com/exchange/v1"
CRYPTOCOM_TICKER = f"{CRYPTOCOM_BASE}/public/get-tickers"
CRYPTOCOM_CANDLES = f"{CRYPTOCOM_BASE}/public/get-candlestick"

PROXY_EXCHANGES = (
    "Coinbase",
    "Kraken",
    "Bitstamp",
    "Gemini",
    "Crypto.com",
)

DEFAULT_CONTEXT_HOURS = 3.0
DEFAULT_PATTERN_DAYS = 7.0
DEFAULT_BACKTEST_HOURS = 48.0
DEFAULT_K_NEIGHBORS = 60
DEFAULT_SIMULATIONS = 12000
DEFAULT_BACKTEST_SIMULATIONS = 1500

# Adaptive model weighting.
# Component weights are learned from the walk-forward backtest scorecard.
#
# Bayesian prior keeps very small samples from getting extreme weights.
DEFAULT_WEIGHT_PRIOR_ROUNDS = 20.0

# Every component keeps a small voice even if recent accuracy is <= 50%.
# Better-than-random backtest edge adds extra weight.
DEFAULT_WEIGHT_BASE_SCORE = 0.01

# How often to refresh the live current-price display during the round.
DEFAULT_LIVE_REFRESH_SECONDS = 3.0

# Target mode:
# manual = ask user for Robinhood's real target each round
# auto   = keep the previous automatic benchmark behavior
DEFAULT_TARGET_MODE = "manual"

SCRIPT_DIR = Path(__file__).resolve().parent
STATS_FILE = SCRIPT_DIR / "data.json"


# ============================================================
# DISPLAY / HELPERS
# ============================================================

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def enable_ansi():
    """
    Best-effort ANSI enable for Windows terminals.
    Safe no-op on other platforms.
    """
    if os.name != "nt":
        return

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()

        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"


def colorize(text, color="", bold=False, dim=False):
    prefix = ""
    if bold:
        prefix += ANSI_BOLD
    if dim:
        prefix += ANSI_DIM
    if color:
        prefix += color

    if not prefix:
        return str(text)

    return f"{prefix}{text}{ANSI_RESET}"


def color_by_direction(label, text=None, bold=True):
    raw = str(label).upper()

    if "UP" in raw or "ABOVE" in raw:
        return colorize(text or label, ANSI_GREEN, bold=bold)

    if "DOWN" in raw or "BELOW" in raw:
        return colorize(text or label, ANSI_RED, bold=bold)

    return colorize(text or label, ANSI_YELLOW, bold=bold)


def color_percentage(probability):
    p = float(probability)

    if p >= 0.55:
        color = ANSI_GREEN
    elif p <= 0.45:
        color = ANSI_RED
    else:
        color = ANSI_YELLOW

    return colorize(f"{p * 100:6.2f}%", color, bold=True)


def color_signed_percent(value):
    color = ANSI_GREEN if value >= 0 else ANSI_RED
    return colorize(f"{value:+.4f}%", color, bold=True)


def color_signed_money(value):
    color = ANSI_GREEN if value >= 0 else ANSI_RED
    return colorize(f"${value:+,.2f}", color, bold=True)


def color_strength(label):
    mapping = {
        "EXTREMELY WEAK": ANSI_YELLOW,
        "VERY WEAK": ANSI_YELLOW,
        "WEAK": ANSI_YELLOW,
        "MODERATE": ANSI_CYAN,
        "STRONG": ANSI_GREEN,
    }

    return colorize(
        label,
        mapping.get(label, ANSI_WHITE),
        bold=True,
    )


def fmt_money(value):
    return f"${value:,.2f}"


def iso_utc(dt):
    return (
        dt.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def local_dt_from_ts(ts):
    return datetime.fromtimestamp(
        ts,
        tz=timezone.utc,
    ).astimezone()


def floor_15m(dt):
    minute = (dt.minute // 15) * 15
    return dt.replace(
        minute=minute,
        second=0,
        microsecond=0,
    )


def current_round(now=None):
    if now is None:
        now = datetime.now().astimezone()

    start = floor_15m(now)
    end = start + timedelta(minutes=15)
    return start, end


def seconds_until(dt):
    return max(
        0.0,
        (dt - datetime.now().astimezone()).total_seconds(),
    )


# ============================================================
# HTTP
# ============================================================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "crypto-prediction-markets-companion/1.0",
    "Accept": "application/json",
})


def http_get_json(url, params=None, auth=None, timeout=10):
    r = SESSION.get(
        url,
        params=params,
        auth=auth,
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


# ============================================================
# PRICE / BENCHMARK SOURCES
# ============================================================

class BenchmarkSource:
    """
    Free multi-exchange BRTI-like proxy.

    Live proxy:
      - fetch BTC/USD spot quotes from several public exchanges
      - use bid/ask midpoint when available
      - reject obvious cross-exchange outliers
      - take the median of the remaining exchange mid-prices

    IMPORTANT:
    This is NOT CME CF BRTI and must not be labeled as official BRTI.
    """

    def __init__(self, requested="proxy", asset=DEFAULT_ASSET):
        self.requested = requested
        self.asset = asset.upper()
        if self.asset not in ASSET_CONFIGS:
            raise ValueError(f"Unsupported asset: {asset}")
        self.config = ASSET_CONFIGS[self.asset]
        self.mode = "proxy"
        self.last_quotes = {}
        self.last_errors = {}

    @property
    def label(self):
        return f"Multi-exchange {self.asset}/USD proxy"

    @staticmethod
    def _mid(bid, ask, last=None):
        try:
            bid = float(bid)
            ask = float(ask)

            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
        except Exception:
            pass

        if last is not None:
            value = float(last)
            if value > 0:
                return value

        raise RuntimeError("No valid quote.")

    def _coinbase_quote(self):
        data = http_get_json(
            f"{COINBASE_BASE}/products/{self.config['coinbase']}/ticker",
            timeout=4,
        )

        return self._mid(
            data.get("bid"),
            data.get("ask"),
            data.get("price"),
        )

    def _bitstamp_quote(self):
        data = http_get_json(
            f"https://www.bitstamp.net/api/v2/ticker/{self.config['bitstamp']}/",
            timeout=4,
        )

        return self._mid(
            data.get("bid"),
            data.get("ask"),
            data.get("last"),
        )

    def _kraken_quote(self):
        data = http_get_json(
            KRAKEN_TICKER,
            params={
                "pair": self.config["kraken"],
            },
            timeout=4,
        )

        result = data.get(
            "result",
            {},
        )

        if not result:
            raise RuntimeError(
                "Kraken returned no ticker result."
            )

        item = next(
            iter(
                result.values()
            )
        )

        ask = item.get(
            "a",
            [None],
        )[0]

        bid = item.get(
            "b",
            [None],
        )[0]

        last = item.get(
            "c",
            [None],
        )[0]

        return self._mid(
            bid,
            ask,
            last,
        )

    def _gemini_quote(self):
        data = http_get_json(
            f"https://api.gemini.com/v2/ticker/{self.config['gemini']}",
            timeout=4,
        )

        return self._mid(
            data.get("bid"),
            data.get("ask"),
            data.get("close"),
        )

    def _cryptocom_quote(self):
        data = http_get_json(
            CRYPTOCOM_TICKER,
            params={
                "instrument_name": self.config["cryptocom"],
            },
            timeout=4,
        )

        result = data.get(
            "result",
            {},
        )

        rows = result.get(
            "data",
            [],
        )

        if not rows:
            raise RuntimeError(
                f"Crypto.com returned no {self.config['cryptocom']} ticker."
            )

        row = rows[0]

        return self._mid(
            row.get("b"),
            row.get("k"),
            row.get("a"),
        )

    def latest(self):
        fetchers = {
            "Coinbase": self._coinbase_quote,
            "Kraken": self._kraken_quote,
            "Bitstamp": self._bitstamp_quote,
            "Gemini": self._gemini_quote,
            "Crypto.com": self._cryptocom_quote,
        }
        fetchers = {
            name: func for name, func in fetchers.items()
            if not (
                (name == "Gemini" and not self.config.get("gemini"))
                or (name == "Crypto.com" and not self.config.get("cryptocom"))
            )
        }

        quotes = {}
        errors = {}

        with ThreadPoolExecutor(
            max_workers=len(fetchers)
        ) as pool:
            futures = {
                pool.submit(func): name
                for name, func
                in fetchers.items()
            }

            for future in as_completed(
                futures
            ):
                name = futures[
                    future
                ]

                try:
                    value = float(
                        future.result()
                    )

                    if (
                        math.isfinite(value)
                        and value > 0
                    ):
                        quotes[
                            name
                        ] = value

                except Exception as exc:
                    errors[
                        name
                    ] = str(exc)

        if len(quotes) < 2:
            raise RuntimeError(
                "Need at least 2 exchange quotes for the proxy. "
                f"Available={list(quotes)} errors={errors}"
            )

        values = np.array(
            list(
                quotes.values()
            ),
            dtype=float,
        )

        center = float(
            np.median(
                values
            )
        )

        # Reject any feed more than 0.50% away from the cross-exchange median.
        filtered = {
            name: value
            for name, value
            in quotes.items()
            if abs(
                value
                / center
                - 1.0
            ) <= 0.005
        }

        if len(filtered) < 2:
            filtered = quotes

        proxy = float(
            np.median(
                np.array(
                    list(
                        filtered.values()
                    ),
                    dtype=float,
                )
            )
        )

        self.last_quotes = dict(
            sorted(
                filtered.items()
            )
        )

        self.last_errors = errors

        return proxy

    def quote_summary(self):
        if not self.last_quotes:
            return ""

        return " | ".join(
            f"{name} {fmt_money(value)}"
            for name, value
            in self.last_quotes.items()
        )




# ============================================================
# MULTI-EXCHANGE HISTORICAL CANDLES
# ============================================================

def fetch_coinbase_candles(start, end, asset=DEFAULT_ASSET):
    """
    Coinbase Exchange 1-minute candles.
    Row: [time, low, high, open, close, volume]
    """
    cursor = start.astimezone(
        timezone.utc
    )

    end = end.astimezone(
        timezone.utc
    )

    chunk = timedelta(
        minutes=290
    )

    rows_by_ts = {}

    while cursor < end:
        chunk_end = min(
            cursor + chunk,
            end,
        )

        rows = http_get_json(
            f"{COINBASE_BASE}/products/{ASSET_CONFIGS[asset]['coinbase']}/candles",
            params={
                "granularity": 60,
                "start": iso_utc(cursor),
                "end": iso_utc(chunk_end),
            },
            timeout=12,
        )

        for row in rows:
            ts = int(
                row[0]
            )

            rows_by_ts[ts] = {
                "time": ts,
                "low": float(row[1]),
                "high": float(row[2]),
                "open": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "exchange": "Coinbase",
            }

        cursor = chunk_end

        if cursor < end:
            time.sleep(
                0.06
            )

    return [
        rows_by_ts[k]
        for k in sorted(
            rows_by_ts
        )
    ]


def fetch_bitstamp_candles(start, end, asset=DEFAULT_ASSET):
    """
    Bitstamp supports 1-minute OHLC with explicit start/end and up to
    1000 rows per request.
    """
    start_ts = int(
        start.astimezone(
            timezone.utc
        ).timestamp()
    )

    end_ts = int(
        end.astimezone(
            timezone.utc
        ).timestamp()
    )

    cursor = start_ts
    rows_by_ts = {}

    while cursor < end_ts:
        chunk_end = min(
            cursor + 900 * 60,
            end_ts,
        )

        minutes = max(
            1,
            int(
                math.ceil(
                    (
                        chunk_end
                        - cursor
                    )
                    / 60
                )
            )
            + 2,
        )

        data = http_get_json(
            f"https://www.bitstamp.net/api/v2/ohlc/{ASSET_CONFIGS[asset]['bitstamp']}/",
            params={
                "step": 60,
                "limit": min(
                    1000,
                    minutes,
                ),
                "start": cursor,
                "end": chunk_end,
                "exclude_current_candle": "true",
            },
            timeout=12,
        )

        rows = (
            data.get(
                "data",
                {},
            )
            .get(
                "ohlc",
                [],
            )
        )

        for row in rows:
            ts = int(
                row[
                    "timestamp"
                ]
            )

            if (
                ts < start_ts
                or ts >= end_ts
            ):
                continue

            rows_by_ts[ts] = {
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0)),
                "exchange": "Bitstamp",
            }

        cursor = chunk_end

        if cursor < end_ts:
            time.sleep(
                0.05
            )

    return [
        rows_by_ts[k]
        for k in sorted(
            rows_by_ts
        )
    ]


def fetch_kraken_candles(start, end, asset=DEFAULT_ASSET):
    """
    Kraken public OHLC. Kraken may limit the amount of history returned
    by one call, so this source is primarily useful for the recent part
    of the multi-exchange proxy.
    """
    start_ts = int(
        start.astimezone(
            timezone.utc
        ).timestamp()
    )

    end_ts = int(
        end.astimezone(
            timezone.utc
        ).timestamp()
    )

    data = http_get_json(
        KRAKEN_OHLC,
        params={
            "pair": ASSET_CONFIGS[asset]["kraken"],
            "interval": 1,
            "since": start_ts,
        },
        timeout=12,
    )

    result = data.get(
        "result",
        {},
    )

    pair_rows = None

    for key, value in result.items():
        if key == "last":
            continue

        if isinstance(
            value,
            list,
        ):
            pair_rows = value
            break

    if pair_rows is None:
        return []

    out = []

    for row in pair_rows:
        try:
            ts = int(
                float(
                    row[0]
                )
            )

            if (
                ts < start_ts
                or ts >= end_ts
            ):
                continue

            out.append({
                "time": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[6]),
                "exchange": "Kraken",
            })
        except Exception:
            continue

    return out


def fetch_gemini_candles(start, end, asset=DEFAULT_ASSET):
    """
    Gemini public 1-minute candles. The endpoint is recent-history oriented,
    so older minutes may not be present.
    """
    start_ts = int(
        start.astimezone(
            timezone.utc
        ).timestamp()
    )

    end_ts = int(
        end.astimezone(
            timezone.utc
        ).timestamp()
    )

    pair = ASSET_CONFIGS[asset].get("gemini")
    if not pair:
        return []
    rows = http_get_json(
        f"https://api.gemini.com/v2/candles/{pair}/1m",
        timeout=12,
    )

    out = []

    for row in rows:
        try:
            ts = int(
                int(
                    row[0]
                )
                / 1000
            )

            if (
                ts < start_ts
                or ts >= end_ts
            ):
                continue

            out.append({
                "time": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "exchange": "Gemini",
            })
        except Exception:
            continue

    return out


def fetch_cryptocom_candles(start, end, asset=DEFAULT_ASSET):
    """
    Crypto.com public recent 1-minute candles.
    """
    start_ts = int(
        start.astimezone(
            timezone.utc
        ).timestamp()
    )

    end_ts = int(
        end.astimezone(
            timezone.utc
        ).timestamp()
    )

    instrument = ASSET_CONFIGS[asset].get("cryptocom")
    if not instrument:
        return []
    data = http_get_json(
        CRYPTOCOM_CANDLES,
        params={
            "instrument_name": instrument,
            "timeframe": "1m",
            "count": 300,
        },
        timeout=12,
    )

    rows = (
        data.get(
            "result",
            {},
        )
        .get(
            "data",
            [],
        )
    )

    out = []

    for row in rows:
        try:
            ts = int(
                int(
                    row["t"]
                )
                / 1000
            )

            if (
                ts < start_ts
                or ts >= end_ts
            ):
                continue

            out.append({
                "time": ts,
                "open": float(row["o"]),
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "volume": float(row.get("v", 0.0)),
                "exchange": "Crypto.com",
            })
        except Exception:
            continue

    return out


def merge_exchange_candles(exchange_data):
    """
    Build one synthetic 1-minute BRTI-like proxy candle.

    Price fields = cross-exchange median.
    Volume       = Coinbase volume when available, otherwise median volume.

    Median is intentionally used to reduce sensitivity to a single exchange
    quote/candle that temporarily diverges.
    """
    by_ts = {}

    for exchange_name, candles in exchange_data.items():
        for candle in candles:
            ts = int(
                candle["time"]
            )

            by_ts.setdefault(
                ts,
                {},
            )[
                exchange_name
            ] = candle

    merged = []

    for ts in sorted(
        by_ts
    ):
        items = by_ts[
            ts
        ]

        if not items:
            continue

        opens = np.array(
            [
                c["open"]
                for c in items.values()
            ],
            dtype=float,
        )

        highs = np.array(
            [
                c["high"]
                for c in items.values()
            ],
            dtype=float,
        )

        lows = np.array(
            [
                c["low"]
                for c in items.values()
            ],
            dtype=float,
        )

        closes = np.array(
            [
                c["close"]
                for c in items.values()
            ],
            dtype=float,
        )

        # Cross-exchange price sanity check around median close.
        close_center = float(
            np.median(
                closes
            )
        )

        good_names = []

        for name, candle in items.items():
            if abs(
                candle["close"]
                / close_center
                - 1.0
            ) <= 0.006:
                good_names.append(
                    name
                )

        if not good_names:
            good_names = list(
                items
            )

        good = [
            items[
                name
            ]
            for name in good_names
        ]

        def med(field):
            return float(
                np.median(
                    np.array(
                        [
                            c[field]
                            for c in good
                        ],
                        dtype=float,
                    )
                )
            )

        if "Coinbase" in items:
            volume = float(
                items[
                    "Coinbase"
                ][
                    "volume"
                ]
            )
        else:
            volume = float(
                np.median(
                    np.array(
                        [
                            c[
                                "volume"
                            ]
                            for c in good
                        ],
                        dtype=float,
                    )
                )
            )

        merged.append({
            "time": ts,
            "open": med("open"),
            "high": med("high"),
            "low": med("low"),
            "close": med("close"),
            "volume": volume,
            "source_count": len(
                good
            ),
            "sources": ",".join(
                sorted(
                    good_names
                )
            ),
        })

    return merged


def fetch_candles_between(start, end, asset=DEFAULT_ASSET):
    """
    Multi-exchange historical proxy.

    Coinbase + Bitstamp normally provide the deeper historical backbone.
    Kraken / Gemini / Crypto.com add recent history when their free public
    endpoints make it available.
    """
    fetchers = {
        "Coinbase": fetch_coinbase_candles,
        "Bitstamp": fetch_bitstamp_candles,
        "Kraken": fetch_kraken_candles,
        "Gemini": fetch_gemini_candles,
        "Crypto.com": fetch_cryptocom_candles,
    }
    config = ASSET_CONFIGS[asset]
    fetchers = {
        name: func for name, func in fetchers.items()
        if not (
            (name == "Gemini" and not config.get("gemini"))
            or (name == "Crypto.com" and not config.get("cryptocom"))
        )
    }

    exchange_data = {}
    errors = {}

    # Historical calls can be large, so run exchanges in parallel.
    with ThreadPoolExecutor(
        max_workers=len(
            fetchers
        )
    ) as pool:
        futures = {
            pool.submit(
                func,
                start,
                end,
                asset,
            ): name
            for name, func
            in fetchers.items()
        }

        for future in as_completed(
            futures
        ):
            name = futures[
                future
            ]

            try:
                candles = (
                    future.result()
                )

                if candles:
                    exchange_data[
                        name
                    ] = candles

                    print(
                        f"  {name:10s}: "
                        f"{len(candles):5d} candles"
                    )
                else:
                    errors[
                        name
                    ] = "no candles returned"

            except Exception as exc:
                errors[
                    name
                ] = str(
                    exc
                )

    if (
        "Coinbase"
        not in exchange_data
        and "Bitstamp"
        not in exchange_data
    ):
        raise RuntimeError(
            "Historical proxy needs at least Coinbase or Bitstamp. "
            f"Errors: {errors}"
        )

    if errors:
        for name, message in errors.items():
            print(
                f"  {name:10s}: unavailable "
                f"({message[:90]})"
            )

    merged = merge_exchange_candles(
        exchange_data
    )

    print(
        f"  Proxy      : "
        f"{len(merged):5d} merged 1m candles "
        f"from {len(exchange_data)} exchange source(s)"
    )

    return merged


def fetch_recent_candles(hours, asset=DEFAULT_ASSET):
    end = datetime.now(
        timezone.utc
    )

    start = (
        end
        - timedelta(
            hours=hours
        )
    )

    return fetch_candles_between(
        start,
        end,
        asset,
    )




# ============================================================
# CANDLE INDEX
# ============================================================

class CandleIndex:
    def __init__(self, candles):
        self.candles = sorted(
            candles,
            key=lambda x: x["time"],
        )

        self.times = np.array(
            [
                c["time"]
                for c in self.candles
            ],
            dtype=np.int64,
        )

        self.opens = np.array(
            [
                c["open"]
                for c in self.candles
            ],
            dtype=float,
        )

        self.highs = np.array(
            [
                c["high"]
                for c in self.candles
            ],
            dtype=float,
        )

        self.lows = np.array(
            [
                c["low"]
                for c in self.candles
            ],
            dtype=float,
        )

        self.closes = np.array(
            [
                c["close"]
                for c in self.candles
            ],
            dtype=float,
        )

        self.volumes = np.array(
            [
                c["volume"]
                for c in self.candles
            ],
            dtype=float,
        )

        self.time_to_index = {
            int(ts): i
            for i, ts
            in enumerate(
                self.times
            )
        }

    def index_before_boundary(self, boundary_ts):
        """
        Returns index of the last completed 1-minute candle
        whose start time is boundary - 60 sec.
        """

        target_ts = int(
            boundary_ts
            - 60
        )

        return self.time_to_index.get(
            target_ts
        )

    def target_proxy_from_previous_minute(self, boundary_ts):
        """
        Fallback estimate when the script did not collect second-by-second
        benchmark samples before a boundary.

        Robinhood uses the official 60-second BRTI average.
        This free multi-exchange proxy cannot reproduce the licensed BRTI
        methodology exactly, so use the proxy minute's OHLC average as an
        explicit ESTIMATE.
        """

        idx = self.time_to_index.get(
            int(
                boundary_ts - 60
            )
        )

        if idx is None:
            return None

        return float(
            (
                self.opens[idx]
                + self.highs[idx]
                + self.lows[idx]
                + self.closes[idx]
            )
            / 4.0
        )


# ============================================================
# FEATURE ENGINE
# ============================================================

FEATURE_NAMES = [
    "ret_3m",
    "ret_5m",
    "ret_15m",
    "ret_30m",
    "ret_60m",
    "ret_180m",
    "vol_5m",
    "vol_15m",
    "vol_30m",
    "vol_60m",
    "range_pos_60m",
    "rsi_14",
    "volume_ratio_15_60",
    "trend_slope_30m",
]


def simple_return(prices, end_idx, minutes):
    start_idx = end_idx - minutes

    if start_idx < 0:
        return None

    start_price = prices[
        start_idx
    ]

    end_price = prices[
        end_idx
    ]

    if start_price <= 0:
        return None

    return float(
        end_price / start_price
        - 1.0
    )


def return_volatility(prices, end_idx, minutes):
    start_idx = (
        end_idx
        - minutes
        + 1
    )

    if start_idx < 1:
        return None

    segment = prices[
        start_idx - 1:
        end_idx + 1
    ]

    if len(segment) < 3:
        return None

    log_returns = np.diff(
        np.log(segment)
    )

    if len(log_returns) < 2:
        return 0.0

    return float(
        np.std(
            log_returns,
            ddof=1,
        )
    )


def rsi_14(prices, end_idx):
    start_idx = (
        end_idx
        - 14
    )

    if start_idx < 0:
        return None

    segment = prices[
        start_idx:
        end_idx + 1
    ]

    changes = np.diff(
        segment
    )

    gains = np.where(
        changes > 0,
        changes,
        0.0,
    )

    losses = np.where(
        changes < 0,
        -changes,
        0.0,
    )

    avg_gain = float(
        np.mean(gains)
    )

    avg_loss = float(
        np.mean(losses)
    )

    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0
        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return float(
        100.0
        - (
            100.0
            / (
                1.0
                + rs
            )
        )
    )


def linear_trend(prices, end_idx, minutes):
    start_idx = (
        end_idx
        - minutes
        + 1
    )

    if start_idx < 0:
        return None

    segment = prices[
        start_idx:
        end_idx + 1
    ]

    if len(segment) < 3:
        return None

    # Log-price linear slope per minute.
    y = np.log(
        segment
    )

    x = np.arange(
        len(segment),
        dtype=float,
    )

    slope = np.polyfit(
        x,
        y,
        1,
    )[0]

    return float(
        slope
    )


def build_features(index, boundary_ts):
    """
    Feature vector using ONLY candles completed BEFORE boundary_ts.
    """

    end_idx = index.index_before_boundary(
        boundary_ts
    )

    if end_idx is None:
        return None

    if end_idx < 181:
        return None

    prices = index.closes
    volumes = index.volumes

    returns = []

    for minutes in (
        3,
        5,
        15,
        30,
        60,
        180,
    ):
        value = simple_return(
            prices,
            end_idx,
            minutes,
        )

        if value is None:
            return None

        returns.append(
            value
        )

    vols = []

    for minutes in (
        5,
        15,
        30,
        60,
    ):
        value = return_volatility(
            prices,
            end_idx,
            minutes,
        )

        if value is None:
            return None

        vols.append(
            value
        )

    start_60 = (
        end_idx
        - 59
    )

    segment_60 = prices[
        start_60:
        end_idx + 1
    ]

    low_60 = float(
        np.min(
            segment_60
        )
    )

    high_60 = float(
        np.max(
            segment_60
        )
    )

    current = float(
        prices[
            end_idx
        ]
    )

    if high_60 > low_60:
        range_pos = (
            current
            - low_60
        ) / (
            high_60
            - low_60
        )
    else:
        range_pos = 0.5

    rsi = rsi_14(
        prices,
        end_idx,
    )

    vol15 = float(
        np.mean(
            volumes[
                end_idx - 14:
                end_idx + 1
            ]
        )
    )

    vol60 = float(
        np.mean(
            volumes[
                end_idx - 59:
                end_idx + 1
            ]
        )
    )

    if vol60 > 0:
        volume_ratio = (
            vol15 / vol60
        )
    else:
        volume_ratio = 1.0

    slope = linear_trend(
        prices,
        end_idx,
        30,
    )

    if (
        rsi is None
        or slope is None
    ):
        return None

    vector = np.array(
        returns
        + vols
        + [
            range_pos,
            rsi / 100.0,
            volume_ratio,
            slope,
        ],
        dtype=float,
    )

    if not np.all(
        np.isfinite(
            vector
        )
    ):
        return None

    return vector


def future_15m_return(index, boundary_ts):
    """
    Future move from the last completed price immediately before boundary
    to the last completed price 15 minutes later.

    Used only for historical training/backtesting, never for live current
    round prediction.
    """

    start_idx = index.index_before_boundary(
        boundary_ts
    )

    end_idx = index.index_before_boundary(
        boundary_ts + 15 * 60
    )

    if (
        start_idx is None
        or end_idx is None
    ):
        return None

    start_price = index.closes[
        start_idx
    ]

    end_price = index.closes[
        end_idx
    ]

    if start_price <= 0:
        return None

    return float(
        end_price / start_price
        - 1.0
    )


# ============================================================
# HISTORICAL PATTERN LIBRARY
# ============================================================

def boundary_timestamps(index):
    values = []

    for ts in index.times:
        dt = datetime.fromtimestamp(
            int(ts),
            tz=timezone.utc,
        )

        # Candle ts is minute START.
        # A boundary exists at minute 00/15/30/45.
        if dt.minute % 15 == 0:
            values.append(
                int(ts)
            )

    return values


def build_pattern_records(index):
    records = []

    for boundary_ts in boundary_timestamps(
        index
    ):
        features = build_features(
            index,
            boundary_ts,
        )

        if features is None:
            continue

        future_ret = future_15m_return(
            index,
            boundary_ts,
        )

        if future_ret is None:
            continue

        records.append({
            "ts": boundary_ts,
            "features": features,
            "future_return": future_ret,
            "up": (
                1.0
                if future_ret >= 0
                else 0.0
            ),
        })

    return records


# ============================================================
# PREDICTION MODEL
# ============================================================

def robust_scale_matrix(matrix):
    center = np.median(
        matrix,
        axis=0,
    )

    q75 = np.percentile(
        matrix,
        75,
        axis=0,
    )

    q25 = np.percentile(
        matrix,
        25,
        axis=0,
    )

    scale = (
        q75 - q25
    )

    std = np.std(
        matrix,
        axis=0,
    )

    scale = np.where(
        scale > 1e-12,
        scale,
        std,
    )

    scale = np.where(
        scale > 1e-12,
        scale,
        1.0,
    )

    return center, scale


def pattern_probability(
    current_features,
    candidate_records,
    k_neighbors,
    required_return=0.0,
):
    """
    Probability that the next 15-minute return reaches/exceeds required_return
    among the nearest historical market states.

    required_return:
        target / round_start_reference - 1

    Example:
        +0.0005 means BTC must rise at least +0.05%
        -0.0003 means BTC can fall 0.03% and still finish ABOVE target
    """

    if len(
        candidate_records
    ) < 10:
        return 0.5, 0, []

    matrix = np.vstack(
        [
            r["features"]
            for r
            in candidate_records
        ]
    )

    center, scale = (
        robust_scale_matrix(
            matrix
        )
    )

    z_matrix = (
        matrix
        - center
    ) / scale

    z_current = (
        current_features
        - center
    ) / scale

    distances = np.sqrt(
        np.mean(
            (
                z_matrix
                - z_current
            ) ** 2,
            axis=1,
        )
    )

    k = min(
        k_neighbors,
        len(
            candidate_records
        ),
    )

    nearest_indices = np.argsort(
        distances
    )[:k]

    nearest_distances = distances[
        nearest_indices
    ]

    weights = np.exp(
        -np.square(
            nearest_distances
        )
        / 2.0
    )

    if float(
        np.sum(
            weights
        )
    ) <= 0:
        weights = np.ones_like(
            weights
        )

    neighbor_returns_array = np.array(
        [
            candidate_records[
                int(i)
            ]["future_return"]
            for i
            in nearest_indices
        ],
        dtype=float,
    )

    target_hits = (
        neighbor_returns_array
        >= required_return
    ).astype(float)

    p_above_target = float(
        np.average(
            target_hits,
            weights=weights,
        )
    )

    return (
        p_above_target,
        k,
        neighbor_returns_array.tolist(),
    )



def recent_regime_probability(
    index,
    boundary_ts,
    context_hours,
    required_return=0.0,
):
    """
    Target-aware probability based on recent completed 15-minute moves.

    Counts how often recent 15-minute moves were large enough to reach
    the current round's required target return.
    """

    boundary_idx = (
        index.index_before_boundary(
            boundary_ts
        )
    )

    if boundary_idx is None:
        return 0.5, 0

    context_minutes = int(
        context_hours
        * 60
    )

    first_idx = max(
        0,
        boundary_idx
        - context_minutes
        + 1,
    )

    moves = []
    ages = []

    last_start_idx = (
        boundary_idx
        - 15
    )

    for start_idx in range(
        first_idx,
        last_start_idx + 1,
    ):
        end_idx = (
            start_idx
            + 15
        )

        if end_idx > boundary_idx:
            break

        start_price = (
            index.closes[
                start_idx
            ]
        )

        end_price = (
            index.closes[
                end_idx
            ]
        )

        if start_price <= 0:
            continue

        move = (
            end_price
            / start_price
            - 1.0
        )

        age = (
            boundary_idx
            - end_idx
        )

        moves.append(
            move
        )

        ages.append(
            age
        )

    if not moves:
        return 0.5, 0

    moves = np.array(
        moves,
        dtype=float,
    )

    ages = np.array(
        ages,
        dtype=float,
    )

    half_life_minutes = max(
        30.0,
        context_minutes / 2.0,
    )

    lam = (
        math.log(2.0)
        / half_life_minutes
    )

    weights = np.exp(
        -lam * ages
    )

    weights /= np.sum(
        weights
    )

    p_above_target = float(
        np.sum(
            (
                moves >= required_return
            ).astype(float)
            * weights
        )
    )

    return (
        p_above_target,
        len(
            moves
        ),
    )



def bootstrap_probability(
    index,
    boundary_ts,
    context_hours,
    simulations,
    seed,
    required_return=0.0,
):
    """
    Bootstrap 15-minute future returns using ONLY one-minute returns known
    before the round starts, then calculate probability of reaching target.
    """

    boundary_idx = (
        index.index_before_boundary(
            boundary_ts
        )
    )

    if boundary_idx is None:
        return 0.5

    context_minutes = int(
        context_hours
        * 60
    )

    start_idx = max(
        1,
        boundary_idx
        - context_minutes
        + 1,
    )

    segment = index.closes[
        start_idx - 1:
        boundary_idx + 1
    ]

    if len(
        segment
    ) < 20:
        return 0.5

    log_returns = np.diff(
        np.log(
            segment
        )
    )

    n = len(
        log_returns
    )

    ages = np.arange(
        n - 1,
        -1,
        -1,
        dtype=float,
    )

    half_life = max(
        30.0,
        n / 2.0,
    )

    weights = np.exp(
        -(
            math.log(2.0)
            / half_life
        )
        * ages
    )

    weights /= np.sum(
        weights
    )

    rng = np.random.default_rng(
        seed
    )

    sampled = rng.choice(
        log_returns,
        size=(
            simulations,
            15,
        ),
        replace=True,
        p=weights,
    )

    simulated_log_returns = np.sum(
        sampled,
        axis=1,
    )

    # Convert current simple-return hurdle to log-return hurdle.
    # Protect against impossible <= -100% values.
    safe_required_return = max(
        required_return,
        -0.999999,
    )

    required_log_return = math.log1p(
        safe_required_return
    )

    return float(
        np.mean(
            simulated_log_returns
            >= required_log_return
        )
    )



def trend_probability(
    current_features,
    required_return=0.0,
):
    """
    Target-aware trend probability.

    The historical momentum score is treated as an expected short-term move,
    while recent volatility defines the scale. The probability is then shifted
    by the return required to reach the user-entered target.

    This component remains only one member of the ensemble and its actual
    weight is learned by the walk-forward scorecard.
    """

    ret_5 = current_features[1]
    ret_15 = current_features[2]
    ret_30 = current_features[3]
    ret_60 = current_features[4]

    minute_vol_15 = max(
        current_features[7],
        1e-7,
    )

    # Momentum-derived expected 15m move.
    expected_move = (
        0.30 * ret_5
        + 0.35 * ret_15
        + 0.20 * ret_30
        + 0.15 * ret_60
    )

    sigma_15m = max(
        minute_vol_15
        * math.sqrt(15.0),
        1e-6,
    )

    z = (
        expected_move
        - required_return
    ) / sigma_15m

    z = float(
        np.clip(
            z,
            -4.0,
            4.0,
        )
    )

    # Standard normal CDF.
    p_above_target = (
        0.5
        * (
            1.0
            + math.erf(
                z
                / math.sqrt(2.0)
            )
        )
    )

    return float(
        np.clip(
            p_above_target,
            0.01,
            0.99,
        )
    )



def predict_boundary(
    index,
    records,
    boundary_ts,
    context_hours,
    pattern_days,
    k_neighbors,
    simulations,
    required_return=0.0,
):
    """
    Target-aware prediction using ONLY information available before boundary_ts.

    required_return is frozen at round start:
        target / round_start_reference - 1
    """

    current_features = build_features(
        index,
        boundary_ts,
    )

    if current_features is None:
        raise RuntimeError(
            "Not enough historical data before the round start."
        )

    earliest_pattern_ts = (
        boundary_ts
        - int(
            pattern_days
            * 86400
        )
    )

    candidates = [
        r
        for r in records
        if (
            r["ts"]
            >= earliest_pattern_ts
            and r["ts"]
            + 15 * 60
            <= boundary_ts
        )
    ]

    (
        p_pattern,
        neighbor_count,
        neighbor_returns,
    ) = pattern_probability(
        current_features=current_features,
        candidate_records=candidates,
        k_neighbors=k_neighbors,
        required_return=required_return,
    )

    (
        p_regime,
        regime_samples,
    ) = recent_regime_probability(
        index=index,
        boundary_ts=boundary_ts,
        context_hours=context_hours,
        required_return=required_return,
    )

    p_bootstrap = bootstrap_probability(
        index=index,
        boundary_ts=boundary_ts,
        context_hours=context_hours,
        simulations=simulations,
        seed=(
            int(
                boundary_ts
            )
            % (
                2**32
                - 1
            )
        ),
        required_return=required_return,
    )

    p_trend = trend_probability(
        current_features=current_features,
        required_return=required_return,
    )

    # This fixed blend is retained only as a diagnostic raw value.
    # The final model uses adaptive weights learned from the walk-forward test.
    p_above_target = (
        0.50 * p_pattern
        + 0.20 * p_regime
        + 0.20 * p_bootstrap
        + 0.10 * p_trend
    )

    p_above_target = float(
        np.clip(
            p_above_target,
            0.01,
            0.99,
        )
    )

    if neighbor_returns:
        nr = np.array(
            neighbor_returns,
            dtype=float,
        )

        neighbor_median_move = float(
            np.median(
                nr
            )
        )

        neighbor_p10 = float(
            np.percentile(
                nr,
                10,
            )
        )

        neighbor_p90 = float(
            np.percentile(
                nr,
                90,
            )
        )
    else:
        neighbor_median_move = 0.0
        neighbor_p10 = 0.0
        neighbor_p90 = 0.0

    return {
        "p_up": p_above_target,
        "p_down": 1.0 - p_above_target,
        "p_pattern": p_pattern,
        "p_regime": p_regime,
        "p_bootstrap": p_bootstrap,
        "p_trend": p_trend,
        "required_return": required_return,
        "neighbor_count": neighbor_count,
        "regime_samples": regime_samples,
        "neighbor_median_move": neighbor_median_move,
        "neighbor_p10": neighbor_p10,
        "neighbor_p90": neighbor_p90,
    }


# ============================================================
# ADAPTIVE MODEL WEIGHTS
# ============================================================

COMPONENT_KEYS = (
    "pattern",
    "regime",
    "bootstrap",
    "trend",
)


def empty_component_stats():
    return {
        key: {
            "hits": 0,
            "misses": 0,
        }
        for key in COMPONENT_KEYS
    }


def calculate_adaptive_weights(
    component_stats,
    prior_rounds=DEFAULT_WEIGHT_PRIOR_ROUNDS,
    base_score=DEFAULT_WEIGHT_BASE_SCORE,
):
    """
    Convert the component backtest scorecard into model weights.

    For each model:
      smoothed_accuracy =
          (hits + 0.5 * prior_rounds) /
          (hits + misses + prior_rounds)

    Only accuracy ABOVE 50% earns extra weight:
      score = base_score + max(smoothed_accuracy - 0.50, 0)

    Then all scores are normalized to 100%.

    This avoids giving a huge weight to a model that happened to go
    2/2 or 3/3, while still rewarding models that consistently beat 50%.
    """

    raw = {}

    for key in COMPONENT_KEYS:
        values = component_stats.get(
            key,
            {
                "hits": 0,
                "misses": 0,
            },
        )

        hits = int(
            values.get(
                "hits",
                0,
            )
        )

        misses = int(
            values.get(
                "misses",
                0,
            )
        )

        total = (
            hits
            + misses
        )

        if total > 0:
            raw_accuracy = (
                hits
                / total
            )
        else:
            raw_accuracy = 0.5

        smoothed_accuracy = (
            hits
            + 0.5 * prior_rounds
        ) / (
            total
            + prior_rounds
        )

        edge = max(
            smoothed_accuracy
            - 0.5,
            0.0,
        )

        score = (
            base_score
            + edge
        )

        raw[key] = {
            "hits": hits,
            "misses": misses,
            "rounds": total,
            "raw_accuracy": raw_accuracy,
            "smoothed_accuracy": smoothed_accuracy,
            "edge_above_50": edge,
            "score": score,
        }

    total_score = sum(
        item["score"]
        for item in raw.values()
    )

    if total_score <= 0:
        total_score = float(
            len(
                COMPONENT_KEYS
            )
        )

        for key in COMPONENT_KEYS:
            raw[key]["score"] = 1.0

    for key in COMPONENT_KEYS:
        raw[key]["weight"] = (
            raw[key]["score"]
            / total_score
        )

    return raw


def component_probabilities(prediction):
    return {
        "pattern": prediction["p_pattern"],
        "regime": prediction["p_regime"],
        "bootstrap": prediction["p_bootstrap"],
        "trend": prediction["p_trend"],
    }


def combine_with_adaptive_weights(
    prediction,
    weight_info,
):
    probabilities = component_probabilities(
        prediction
    )

    combined = 0.0

    for key in COMPONENT_KEYS:
        combined += (
            probabilities[key]
            * weight_info[key]["weight"]
        )

    return float(
        np.clip(
            combined,
            0.01,
            0.99,
        )
    )


def confidence_strength(probability):
    """
    Always choose UP or DOWN, but tell the user how weak/strong the edge is.
    """

    confidence = max(
        probability,
        1.0 - probability,
    )

    edge = (
        confidence
        - 0.5
    )

    if edge < 0.01:
        return "EXTREMELY WEAK"
    if edge < 0.02:
        return "VERY WEAK"
    if edge < 0.05:
        return "WEAK"
    if edge < 0.10:
        return "MODERATE"

    return "STRONG"


# ============================================================
# BACKTEST
# ============================================================

def run_backtest(
    index,
    records,
    live_boundary_ts,
    context_hours,
    pattern_days,
    k_neighbors,
    backtest_hours,
    simulations,
    required_return=0.0,
    weight_prior_rounds=DEFAULT_WEIGHT_PRIOR_ROUNDS,
    weight_base_score=DEFAULT_WEIGHT_BASE_SCORE,
):
    """
    Target-aware walk-forward adaptive backtest.

    The LIVE target distance is applied as the same percentage hurdle to every
    historical round:

        synthetic historical target
            = historical start reference * (1 + required_return)

    Example:
        Current target requires +0.06%.
        A historical round counts as ABOVE only if that historical 15-minute
        return reached at least +0.06%.

    Weight learning is still walk-forward: each test round uses only component
    performance from earlier tested rounds.
    """

    test_start_ts = (
        live_boundary_ts
        - int(
            backtest_hours
            * 3600
        )
    )

    test_records = [
        r
        for r in records
        if (
            r["ts"]
            >= test_start_ts
            and r["ts"]
            + 15 * 60
            <= live_boundary_ts
        )
    ]

    test_records = sorted(
        test_records,
        key=lambda r: r["ts"],
    )

    hits = 0
    misses = 0

    component_stats = (
        empty_component_stats()
    )

    confidence_values = []
    brier_values = []
    probability_bins = []

    for record in test_records:
        try:
            prediction = predict_boundary(
                index=index,
                records=records,
                boundary_ts=record["ts"],
                context_hours=context_hours,
                pattern_days=pattern_days,
                k_neighbors=k_neighbors,
                simulations=simulations,
                required_return=required_return,
            )
        except Exception:
            continue

        weights_before = calculate_adaptive_weights(
            component_stats=component_stats,
            prior_rounds=weight_prior_rounds,
            base_score=weight_base_score,
        )

        adaptive_p_above = combine_with_adaptive_weights(
            prediction=prediction,
            weight_info=weights_before,
        )

        predicted_above = (
            adaptive_p_above
            >= 0.5
        )

        actual_above = (
            record[
                "future_return"
            ]
            >= required_return
        )

        if predicted_above == actual_above:
            hits += 1
        else:
            misses += 1

        confidence_values.append(
            max(
                adaptive_p_above,
                1.0 - adaptive_p_above,
            )
        )

        actual_numeric = (
            1.0
            if actual_above
            else 0.0
        )

        brier_values.append(
            (
                adaptive_p_above
                - actual_numeric
            ) ** 2
        )

        probability_bins.append(
            (
                adaptive_p_above,
                actual_numeric,
            )
        )

        probabilities = component_probabilities(
            prediction
        )

        for key in COMPONENT_KEYS:
            component_predicted_above = (
                probabilities[key]
                >= 0.5
            )

            if component_predicted_above == actual_above:
                component_stats[key]["hits"] += 1
            else:
                component_stats[key]["misses"] += 1

    total = (
        hits
        + misses
    )

    final_weights = calculate_adaptive_weights(
        component_stats=component_stats,
        prior_rounds=weight_prior_rounds,
        base_score=weight_base_score,
    )

    component_scorecard = {}

    for key in COMPONENT_KEYS:
        values = final_weights[key]

        component_scorecard[key] = {
            "hits": values["hits"],
            "misses": values["misses"],
            "rounds": values["rounds"],
            "accuracy_pct": (
                values["raw_accuracy"]
                * 100.0
            ),
            "smoothed_accuracy_pct": (
                values["smoothed_accuracy"]
                * 100.0
            ),
            "weight_pct": (
                values["weight"]
                * 100.0
            ),
        }

    if total == 0:
        return {
            "rounds": 0,
            "hits": 0,
            "misses": 0,
            "accuracy_pct": 0.0,
            "avg_confidence_pct": 0.0,
            "brier": None,
            "calibration": [],
            "component_stats": component_scorecard,
            "adaptive_weights": final_weights,
            "required_return": required_return,
        }

    # 5-percentage-point calibration buckets.
    calibration = []

    for lower in np.arange(
        0.0,
        1.0,
        0.05,
    ):
        upper = (
            lower
            + 0.05
        )

        bucket = [
            (
                p,
                actual,
            )
            for p, actual
            in probability_bins
            if (
                p >= lower
                and (
                    p < upper
                    or (
                        upper >= 1.0
                        and p <= upper
                    )
                )
            )
        ]

        if bucket:
            model_avg = float(
                np.mean(
                    [
                        p
                        for p, _
                        in bucket
                    ]
                )
            )

            actual_avg = float(
                np.mean(
                    [
                        actual
                        for _, actual
                        in bucket
                    ]
                )
            )

            calibration.append({
                "low": float(
                    lower
                ),
                "high": float(
                    upper
                ),
                "n": len(
                    bucket
                ),
                "model_avg": model_avg,
                "actual_up_rate": actual_avg,
            })

    return {
        "rounds": total,
        "hits": hits,
        "misses": misses,
        "accuracy_pct": (
            hits
            / total
            * 100.0
        ),
        "avg_confidence_pct": (
            float(
                np.mean(
                    confidence_values
                )
            )
            * 100.0
        ),
        "brier": float(
            np.mean(
                brier_values
            )
        ),
        "calibration": calibration,
        "component_stats": component_scorecard,
        "adaptive_weights": final_weights,
        "required_return": required_return,
    }



def calibrate_live_probability(
    raw_p_up,
    backtest,
):
    """
    Light calibration based on the backtest bucket.

    With few examples, preserve most of raw probability.
    """

    calibration = backtest.get(
        "calibration",
        []
    )

    selected = None

    for bucket in calibration:
        if (
            raw_p_up
            >= bucket["low"]
            and raw_p_up
            < bucket["high"]
        ):
            selected = bucket
            break

    if selected is None:
        return raw_p_up, 0

    n = selected["n"]

    observed = selected[
        "actual_up_rate"
    ]

    prior_strength = 20.0

    calibrated = (
        raw_p_up
        * prior_strength
        + observed
        * n
    ) / (
        prior_strength
        + n
    )

    return (
        float(
            np.clip(
                calibrated,
                0.02,
                0.98,
            )
        ),
        n,
    )


def build_live_recommendation(
    round_info,
    current_price,
    now,
    index,
    context_hours,
    simulations=6000,
):
    """Re-evaluate the likely settlement direction during an active round.

    The original round prediction remains frozen and is still the value scored
    at settlement.  This recommendation is conditioned on the latest price and
    bootstraps only the remaining one-minute moves from pre-round history.
    """
    if current_price is None or current_price <= 0:
        return None

    total_seconds = max(1.0, (round_info["end"] - round_info["start"]).total_seconds())
    elapsed_seconds = float(np.clip(
        (now - round_info["start"]).total_seconds(), 0.0, total_seconds
    ))
    remaining_seconds = max(0.0, (round_info["end"] - now).total_seconds())
    remaining_minutes = int(math.ceil(remaining_seconds / 60.0))

    start_reference = float(round_info["start_reference"])
    target = float(round_info["target"])
    change_dollars = float(current_price - start_reference)
    change_pct = change_dollars / start_reference * 100.0
    target_gap = float(current_price - target)

    if remaining_minutes <= 0:
        p_up = 1.0 if current_price >= target else 0.0
    else:
        boundary_ts = int(round_info["start"].astimezone(timezone.utc).timestamp())
        boundary_idx = index.index_before_boundary(boundary_ts)
        context_minutes = max(20, int(context_hours * 60))

        if boundary_idx is None:
            conditional_p_up = 0.5
        else:
            first_idx = max(1, boundary_idx - context_minutes + 1)
            segment = index.closes[first_idx - 1:boundary_idx + 1]
            log_returns = np.diff(np.log(segment))

            if len(log_returns) < 20:
                conditional_p_up = 0.5
            else:
                ages = np.arange(len(log_returns) - 1, -1, -1, dtype=float)
                half_life = max(30.0, len(log_returns) / 2.0)
                weights = np.exp(-(math.log(2.0) / half_life) * ages)
                weights /= weights.sum()
                minute_number = int(elapsed_seconds // 60)
                seed = (boundary_ts + minute_number * 7919) % (2**32 - 1)
                rng = np.random.default_rng(seed)
                sampled = rng.choice(
                    log_returns,
                    size=(max(500, int(simulations)), remaining_minutes),
                    replace=True,
                    p=weights,
                )
                ending_prices = current_price * np.exp(sampled.sum(axis=1))
                conditional_p_up = float(np.mean(ending_prices >= target))

        # Early in the round retain some weight from the start model; as live
        # evidence accumulates, the price-conditioned estimate takes over.
        progress = elapsed_seconds / total_seconds
        live_weight = 0.35 + 0.65 * progress
        p_up = (
            (1.0 - live_weight) * float(round_info["p_up"])
            + live_weight * conditional_p_up
        )

    p_up = float(np.clip(p_up, 0.0, 1.0))
    direction = "UP / ABOVE" if p_up >= 0.5 else "DOWN / BELOW"
    confidence = max(p_up, 1.0 - p_up)
    original = round_info["prediction"]

    return {
        "updated_at": now,
        "minute": min(15, int(elapsed_seconds // 60) + 1),
        "remaining_minutes": remaining_minutes,
        "current_price": float(current_price),
        "change_dollars": change_dollars,
        "change_pct": change_pct,
        "target_gap": target_gap,
        "p_up": p_up,
        "p_down": 1.0 - p_up,
        "recommendation": direction,
        "confidence": confidence,
        "strength": confidence_strength(p_up),
        "changed": direction != original,
        "status": "CHANGED" if direction != original else "STILL THE SAME",
        "original_prediction": original,
    }


# ============================================================
# LIVE ROUND MONITOR
# ============================================================

def monitor_round_until_last_minute(
    round_info,
    stats,
    source,
    index,
    context_hours,
    reconstructed,
    refresh_seconds,
):
    """
    Show the current market price during the round without changing
    the frozen probability/signal.

    Stops when 60 seconds remain so the benchmark-capture routine can
    take over for the final minute.
    """

    capture_start = (
        round_info["end"]
        - timedelta(seconds=60)
    )

    live_recommendation = None
    recommendation_minute = None

    while True:
        now = datetime.now().astimezone()

        if now >= capture_start:
            try:
                current_market_price = source.latest()
            except Exception:
                current_market_price = None

            if current_market_price is not None:
                live_recommendation = build_live_recommendation(
                    round_info=round_info,
                    current_price=current_market_price,
                    now=now,
                    index=index,
                    context_hours=context_hours,
                )
                print_round(
                    round_info=round_info,
                    stats=stats,
                    source=source,
                    reconstructed=reconstructed,
                    current_market_price=current_market_price,
                    live_recommendation=live_recommendation,
                )
            break

        try:
            current_market_price = source.latest()
        except Exception:
            current_market_price = None

        current_minute = max(0, int((now - round_info["start"]).total_seconds() // 60))
        if current_market_price is not None and current_minute != recommendation_minute:
            live_recommendation = build_live_recommendation(
                round_info=round_info,
                current_price=current_market_price,
                now=now,
                index=index,
                context_hours=context_hours,
            )
            recommendation_minute = current_minute

        print_round(
            round_info=round_info,
            stats=stats,
            source=source,
            reconstructed=reconstructed,
            current_market_price=current_market_price,
            live_recommendation=live_recommendation,
        )

        remaining_to_capture = (
            capture_start - now
        ).total_seconds()

        if remaining_to_capture <= 0:
            break

        time.sleep(
            min(
                refresh_seconds,
                max(
                    0.25,
                    remaining_to_capture,
                ),
            )
        )


# ============================================================
# BENCHMARK CAPTURE
# ============================================================

def sample_last_minute_before_boundary(
    source,
    boundary,
    show_status=True,
):
    """
    Samples approximately once per second during the 60 seconds immediately
    preceding the boundary.

    For BRTI this approximates the official 60-second average directly.
    For Coinbase it is a proxy.
    """

    samples = []

    capture_start = (
        boundary
        - timedelta(
            seconds=60
        )
    )

    # Wait until capture window if called early.
    while True:
        now = datetime.now().astimezone()

        if now >= capture_start:
            break

        remaining = (
            capture_start - now
        ).total_seconds()

        time.sleep(
            min(
                1.0,
                max(
                    0.05,
                    remaining,
                ),
            )
        )

    last_second = None

    while True:
        now = datetime.now().astimezone()

        if now >= boundary:
            break

        second_key = int(
            now.timestamp()
        )

        if (
            second_key
            != last_second
        ):
            try:
                value = source.latest()

                samples.append(
                    (
                        second_key,
                        value,
                    )
                )

                last_second = (
                    second_key
                )

                if show_status:
                    seconds_left = int(
                        max(
                            0,
                            (
                                boundary
                                - now
                            ).total_seconds(),
                        )
                    )

                    print(
                        (
                            f"\rLIVE {fmt_money(value)} "
                            f"| Capturing {source.label} "
                            f"| samples={len(samples):02d} "
                            f"| {seconds_left:02d}s left "
                        ),
                        end="",
                        flush=True,
                    )

            except Exception as exc:
                if show_status:
                    print(
                        (
                            f"\rBenchmark sample error: "
                            f"{str(exc)[:60]:60s}"
                        ),
                        end="",
                        flush=True,
                    )

        # Tight enough to hit roughly each second without busy-waiting.
        time.sleep(
            0.08
        )

    if show_status:
        print()

    if not samples:
        return None, 0

    values = np.array(
        [
            value
            for _, value
            in samples
        ],
        dtype=float,
    )

    return (
        float(
            np.mean(
                values
            )
        ),
        len(
            values
        ),
    )


# ============================================================
# STATS
# ============================================================

def stats_file_for_asset(asset=DEFAULT_ASSET):
    asset = asset.upper()
    return STATS_FILE if asset == "BTC" else SCRIPT_DIR / f"data_{asset.lower()}.json"


def load_stats(asset=DEFAULT_ASSET):
    default = {
        "_asset": asset.upper(),
        "rounds": [],
        "total": 0,
        "hits": 0,
        "misses": 0,
    }

    stats_file = stats_file_for_asset(asset)
    if not stats_file.exists():
        return default

    try:
        data = json.loads(
            stats_file.read_text(
                encoding="utf-8"
            )
        )

        for key, value in default.items():
            data.setdefault(
                key,
                value,
            )

        return data

    except Exception:
        return default


def save_stats(stats):
    stats["rounds"] = (
        stats.get(
            "rounds",
            [],
        )[-1000:]
    )

    stats_file = stats_file_for_asset(stats.get("_asset", DEFAULT_ASSET))
    tmp = stats_file.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            stats,
            indent=2,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        stats_file
    )


def accuracy(stats):
    total = int(
        stats.get(
            "total",
            0,
        )
    )

    if total <= 0:
        return None

    return (
        int(
            stats.get(
                "hits",
                0,
            )
        )
        / total
        * 100.0
    )


def record_result(
    stats,
    round_info,
    settlement,
    actual_direction,
    hit,
):
    stats["total"] += 1

    if hit:
        stats["hits"] += 1
    else:
        stats["misses"] += 1

    stats["rounds"].append({
        "round_start": round_info[
            "start"
        ].isoformat(),
        "round_end": round_info[
            "end"
        ].isoformat(),
        "target": round_info[
            "target"
        ],
        "target_quality": round_info[
            "target_quality"
        ],
        "start_reference": round_info[
            "start_reference"
        ],
        "start_reference_quality": round_info[
            "start_reference_quality"
        ],
        "required_return": round_info[
            "required_return"
        ],
        "p_up": round_info[
            "p_up"
        ],
        "p_down": round_info[
            "p_down"
        ],
        "prediction": round_info[
            "prediction"
        ],
        "strength": round_info[
            "strength"
        ],
        "adaptive_weights": {
            key: round_info[
                "adaptive_weights"
            ][key]["weight"]
            for key in COMPONENT_KEYS
        },
        "settlement": settlement,
        "actual": actual_direction,
        "hit": hit,
        "component_results": {
            name: {
                "predicted": item["predicted"],
                "p_up": item["p_up"],
                "hit": item["hit"],
            }
            for name, item in component_round_results(
                round_info,
                actual_direction,
            ).items()
        },
    })

    save_stats(
        stats
    )


# ============================================================
# TARGET INPUT
# ============================================================

def ask_manual_target(start, end):
    """
    Ask only for the actual Robinhood target.
    This target is used for display and settlement comparison only.
    It does NOT change the frozen historical UP/DOWN probability.
    """
    while True:
        raw = input(
            f"\nEnter Robinhood target for "
            f"{start.strftime('%H:%M')} -> {end.strftime('%H:%M')}: $"
        ).strip()

        if raw.lower() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt

        raw = (
            raw
            .replace(",", "")
            .replace("$", "")
        )

        try:
            value = float(raw)

            if value <= 0:
                raise ValueError

            return value

        except ValueError:
            print(
                "Invalid target. Example: 84834.27"
            )


# ============================================================
# ROUND BUILDING
# ============================================================

def round_start_reference_from_history(
    index,
    boundary,
):
    """
    Historical/proxy BTC reference frozen at the round boundary.

    When the script is opened after the round has started, this reconstructs
    the start reference from the minute immediately before the boundary.
    """

    boundary_ts = int(
        boundary.astimezone(
            timezone.utc
        ).timestamp()
    )

    value = index.target_proxy_from_previous_minute(
        boundary_ts
    )

    if value is None:
        raise RuntimeError(
            "Could not reconstruct BTC round-start reference."
        )

    return (
        float(value),
        "RECONSTRUCTED pre-start 1m proxy",
    )



def get_or_estimate_target(
    index,
    boundary,
    captured_value=None,
    captured_samples=0,
):
    if (
        captured_value is not None
        and captured_samples >= 30
    ):
        return (
            captured_value,
            (
                "CAPTURED "
                f"({captured_samples} second-samples)"
            ),
        )

    boundary_ts = int(
        boundary.astimezone(
            timezone.utc
        ).timestamp()
    )

    estimated = (
        index.target_proxy_from_previous_minute(
            boundary_ts
        )
    )

    if estimated is None:
        raise RuntimeError(
            "Could not estimate round-start benchmark."
        )

    return (
        estimated,
        "ESTIMATED from multi-exchange pre-start 1m OHLC",
    )


def build_round_prediction(
    index,
    records,
    start,
    end,
    target,
    target_quality,
    args,
    start_reference=None,
    start_reference_quality=None,
):
    boundary_ts = int(
        start.astimezone(
            timezone.utc
        ).timestamp()
    )

    if start_reference is None:
        (
            start_reference,
            start_reference_quality,
        ) = round_start_reference_from_history(
            index=index,
            boundary=start,
        )

    if start_reference <= 0:
        raise RuntimeError(
            "Invalid round-start BTC reference."
        )

    required_return = (
        target
        / start_reference
        - 1.0
    )

    raw_prediction = predict_boundary(
        index=index,
        records=records,
        boundary_ts=boundary_ts,
        context_hours=args.context_hours,
        pattern_days=args.pattern_days,
        k_neighbors=args.neighbors,
        simulations=args.simulations,
        required_return=required_return,
    )

    backtest = run_backtest(
        index=index,
        records=records,
        live_boundary_ts=boundary_ts,
        context_hours=args.context_hours,
        pattern_days=args.pattern_days,
        k_neighbors=args.neighbors,
        backtest_hours=args.backtest_hours,
        simulations=args.backtest_simulations,
        required_return=required_return,
        weight_prior_rounds=args.weight_prior_rounds,
        weight_base_score=args.weight_base_score,
    )

    weight_info = backtest.get(
        "adaptive_weights"
    )

    if not weight_info:
        weight_info = calculate_adaptive_weights(
            component_stats=empty_component_stats(),
            prior_rounds=args.weight_prior_rounds,
            base_score=args.weight_base_score,
        )

    adaptive_raw_p_up = combine_with_adaptive_weights(
        prediction=raw_prediction,
        weight_info=weight_info,
    )

    (
        calibrated_p_up,
        calibration_samples,
    ) = calibrate_live_probability(
        adaptive_raw_p_up,
        backtest,
    )

    p_down = (
        1.0
        - calibrated_p_up
    )

    if calibrated_p_up >= 0.5:
        prediction = "UP / ABOVE"
        confidence = calibrated_p_up
    else:
        prediction = "DOWN / BELOW"
        confidence = p_down

    component_probs = component_probabilities(
        raw_prediction
    )

    final_is_up = (
        prediction
        == "UP / ABOVE"
    )

    component_agreement = sum(
        (
            component_probs[key] >= 0.5
        )
        == final_is_up
        for key in COMPONENT_KEYS
    )

    return {
        "start": start,
        "end": end,

        "target": target,
        "target_quality": target_quality,

        "start_reference": start_reference,
        "start_reference_quality": start_reference_quality,

        "required_return": required_return,
        "required_move_dollars": (
            target
            - start_reference
        ),

        "p_up": calibrated_p_up,
        "p_down": p_down,
        "prediction": prediction,
        "confidence": confidence,
        "strength": confidence_strength(
            calibrated_p_up
        ),

        "adaptive_raw_p_up": adaptive_raw_p_up,

        "p_pattern": raw_prediction[
            "p_pattern"
        ],

        "p_regime": raw_prediction[
            "p_regime"
        ],

        "p_bootstrap": raw_prediction[
            "p_bootstrap"
        ],

        "p_trend": raw_prediction[
            "p_trend"
        ],

        "neighbor_count": raw_prediction[
            "neighbor_count"
        ],

        "regime_samples": raw_prediction[
            "regime_samples"
        ],

        "neighbor_median_move": raw_prediction[
            "neighbor_median_move"
        ],

        "neighbor_p10": raw_prediction[
            "neighbor_p10"
        ],

        "neighbor_p90": raw_prediction[
            "neighbor_p90"
        ],

        "component_agreement": component_agreement,
        "adaptive_weights": weight_info,

        "backtest": backtest,
        "calibration_samples": (
            calibration_samples
        ),
    }


# ============================================================
# COMPONENT SCORE HELPERS
# ============================================================

def probability_direction(p_up):
    return "UP / ABOVE TARGET" if p_up >= 0.5 else "DOWN / BELOW TARGET"


def component_round_results(round_info, actual_direction):
    """
    Score every sub-model for this completed round.
    """
    components = {
        "Similar-pattern": round_info["p_pattern"],
        "Recent 3h regime": round_info["p_regime"],
        "Historical bootstrap": round_info["p_bootstrap"],
        "Trend/regime": round_info["p_trend"],
    }

    results = {}

    for name, p_up in components.items():
        predicted = probability_direction(p_up)

        predicted_normalized = (
            "UP / ABOVE"
            if p_up >= 0.5
            else "DOWN / BELOW"
        )

        hit = (
            predicted_normalized
            == actual_direction
        )

        results[name] = {
            "p_up": p_up,
            "predicted": predicted,
            "hit": hit,
        }

    return results


# ============================================================
# UI
# ============================================================

def print_round(
    round_info,
    stats,
    source,
    reconstructed=False,
    current_market_price=None,
    live_recommendation=None,
):
    clear_screen()

    now = datetime.now().astimezone()
    backtest = round_info["backtest"]
    acc = accuracy(stats)

    title = f" ROBINHOOD CRYPTO PREDICTION COMPANION — {source.asset}/USD "
    print(colorize("=" * 76, ANSI_CYAN, bold=True))
    print(colorize(title, ANSI_CYAN, bold=True))
    print(colorize("=" * 76, ANSI_CYAN, bold=True))
    print()

    mode_text = (
        "RECONSTRUCTED"
        if reconstructed
        else "FROZEN"
    )

    print(
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}   "
        f"| Round: {round_info['start'].strftime('%H:%M')} -> {round_info['end'].strftime('%H:%M')}   "
        f"| Mode: {colorize(mode_text, ANSI_MAGENTA, bold=True)}"
    )

    print(
        f"Target: {colorize(fmt_money(round_info['target']), ANSI_CYAN, bold=True)}   "
        f"| Start Ref: {fmt_money(round_info['start_reference'])}   "
        f"| Hurdle: {color_signed_money(round_info['required_move_dollars'])} "
        f"({color_signed_percent(round_info['required_return'] * 100)})"
    )

    if current_market_price is not None:
        live_delta = current_market_price - round_info["target"]
        live_delta_pct = (live_delta / round_info["target"] * 100.0)

        remaining_now = max(
            0.0,
            (round_info["end"] - datetime.now().astimezone()).total_seconds(),
        )
        rem_min = int(remaining_now // 60)
        rem_sec = int(remaining_now % 60)

        live_position = (
            "ABOVE"
            if live_delta > 0
            else "BELOW"
            if live_delta < 0
            else "AT"
        )

        print(
            f"Live: {colorize(fmt_money(current_market_price), ANSI_WHITE, bold=True)}   "
            f"| Δ vs target: {color_signed_money(live_delta)} "
            f"({color_signed_percent(live_delta_pct)}) {color_by_direction(live_position, live_position)}   "
            f"| Left: {colorize(f'{rem_min:02d}:{rem_sec:02d}', ANSI_YELLOW, bold=True)}"
        )

    quote_summary = source.quote_summary() if hasattr(source, "quote_summary") else ""
    if quote_summary:
        print(
            f"Inputs: {colorize(quote_summary, ANSI_WHITE, dim=True)}"
        )

    print(colorize("-" * 76, ANSI_CYAN))

    print(
        f"ABOVE: {color_percentage(round_info['p_up'])}   "
        f"| BELOW: {color_percentage(round_info['p_down'])}"
    )

    decision_text = (
        f"{round_info['prediction']} ({round_info['confidence'] * 100:.2f}%)"
    )
    print(
        f"Decision: {color_by_direction(round_info['prediction'], decision_text)}   "
        f"| Strength: {color_strength(round_info['strength'])}   "
        f"| Agree: {colorize(str(round_info['component_agreement']) + '/4', ANSI_MAGENTA, bold=True)}"
    )

    if live_recommendation is not None:
        rec = live_recommendation
        rec_text = f"{rec['recommendation']} ({rec['confidence'] * 100:.2f}%)"
        status_color = ANSI_RED if rec["changed"] else ANSI_GREEN
        print(
            f"Minute {rec['minute']:02d} live recommendation: "
            f"{color_by_direction(rec['recommendation'], rec_text)}   "
            f"| {colorize(rec['status'], status_color, bold=True)}"
        )
        print(
            f"Since round start: {color_signed_money(rec['change_dollars'])} "
            f"({color_signed_percent(rec['change_pct'])})   "
            f"| Remaining: {rec['remaining_minutes']} min"
        )

    print(colorize("-" * 76, ANSI_CYAN))
    print(colorize("MODELS", ANSI_CYAN, bold=True))

    model_rows = [
        ("pattern", "Pattern", round_info["p_pattern"], round_info["neighbor_count"]),
        ("regime", "Recent", round_info["p_regime"], round_info["regime_samples"]),
        ("bootstrap", "Bootstrap", round_info["p_bootstrap"], None),
        ("trend", "Trend", round_info["p_trend"], None),
    ]

    for key, label, prob, extra in model_rows:
        weight_item = round_info["adaptive_weights"][key]
        bt_acc = weight_item["smoothed_accuracy"] * 100.0
        extra_text = f" | n={extra}" if extra is not None else ""

        print(
            f"{label:10s} "
            f"{color_percentage(prob):>16s} "
            f"| wt {colorize(f'{weight_item['weight'] * 100:5.1f}%', ANSI_BLUE, bold=True)} "
            f"| bt {colorize(f'{bt_acc:5.1f}%', ANSI_WHITE, bold=True)}"
            f"{extra_text}"
        )

    if backtest["brier"] is None:
        brier_text = "N/A"
    else:
        brier_text = colorize(f"{backtest['brier']:.4f}", ANSI_WHITE, bold=True)

    bt_acc_color = ANSI_GREEN if backtest["accuracy_pct"] >= 50 else ANSI_RED

    print(
        f"Backtest: {colorize(str(backtest['rounds']), ANSI_WHITE, bold=True)} rounds   "
        f"| Acc {colorize(f'{backtest['accuracy_pct']:.2f}%', bt_acc_color, bold=True)}   "
        f"| Brier {brier_text}   "
        f"| Cal {colorize(str(round_info['calibration_samples']), ANSI_WHITE, bold=True)}"
    )

    print(
        f"Moves: P10 {color_signed_percent(round_info['neighbor_p10'] * 100)}   "
        f"| Med {color_signed_percent(round_info['neighbor_median_move'] * 100)}   "
        f"| P90 {color_signed_percent(round_info['neighbor_p90'] * 100)}"
    )

    if acc is None:
        tracked = colorize("No completed rounds yet", ANSI_YELLOW, bold=True)
    else:
        tracked = colorize(
            f"{acc:.2f}% ({stats['hits']} HIT / {stats['misses']} MISS)",
            ANSI_GREEN if acc >= 50 else ANSI_RED,
            bold=True,
        )

    print(f"Tracked accuracy: {tracked}")
    print()
    print(
        colorize(
            "Proxy only — Robinhood official BRTI settlement can differ.",
            ANSI_YELLOW,
            bold=True,
        )
    )


def print_settlement(
    round_info,
    settlement,
    settlement_quality,
    actual,
    hit,
    stats,
):
    clear_screen()

    acc = accuracy(stats)

    title = " ROUND PROXY SETTLEMENT "
    print(colorize("=" * 76, ANSI_CYAN, bold=True))
    print(colorize(title, ANSI_CYAN, bold=True))
    print(colorize("=" * 76, ANSI_CYAN, bold=True))
    print()

    print(
        f"Round: {round_info['start'].strftime('%H:%M')} -> {round_info['end'].strftime('%H:%M')}"
    )

    delta = settlement - round_info["target"]
    delta_pct = delta / round_info["target"] * 100.0

    print(
        f"Target: {colorize(fmt_money(round_info['target']), ANSI_CYAN, bold=True)}   "
        f"| Settlement: {colorize(fmt_money(settlement), ANSI_WHITE, bold=True)}   "
        f"| Δ: {color_signed_money(delta)} ({color_signed_percent(delta_pct)})"
    )

    pred_text = f"{round_info['prediction']} ({round_info['confidence'] * 100:.2f}%)"
    print(
        f"Prediction: {color_by_direction(round_info['prediction'], pred_text)}   "
        f"| Actual: {color_by_direction(actual)}   "
        f"| Result: {colorize('HIT ✓' if hit else 'MISS ✗', ANSI_GREEN if hit else ANSI_RED, bold=True)}"
    )

    component_results = component_round_results(round_info, actual)

    pieces = []
    for component_name, result in component_results.items():
        short = {
            "Similar-pattern": "Pattern",
            "Recent 3h regime": "Recent",
            "Historical bootstrap": "Bootstrap",
            "Trend/regime": "Trend",
        }.get(component_name, component_name)

        pieces.append(
            f"{short} "
            f"{colorize('✓' if result['hit'] else '✗', ANSI_GREEN if result['hit'] else ANSI_RED, bold=True)}"
        )

    print("Models this round: " + " | ".join(pieces))

    if acc is not None:
        tracked = colorize(
            f"{acc:.2f}% ({stats['hits']} HIT / {stats['misses']} MISS)",
            ANSI_GREEN if acc >= 50 else ANSI_RED,
            bold=True,
        )
        print(f"Tracked accuracy: {tracked}")

    print(
        f"Settlement source: {colorize(settlement_quality, ANSI_WHITE, bold=True)}"
    )

    print()
    print(
        colorize(
            "Settlement compared against the Robinhood target entered for this round.",
            ANSI_YELLOW,
            bold=True,
        )
    )


# ============================================================
# HISTORY REFRESH
# ============================================================

def load_model_history(args):
    """
    Fetch enough history for:
      pattern library
      recent-context model
      backtest
    """

    hours_needed = max(
        args.pattern_days * 24.0
        + 6.0,
        args.backtest_hours
        + args.context_hours
        + 6.0,
    )

    print(
        f"Loading {hours_needed:.1f} hours of multi-exchange 1-minute {args.asset} history..."
    )

    candles = fetch_recent_candles(
        hours_needed,
        args.asset,
    )

    if len(
        candles
    ) < 300:
        raise RuntimeError(
            "Not enough historical candles returned."
        )

    index = CandleIndex(
        candles
    )

    records = build_pattern_records(
        index
    )

    return (
        candles,
        index,
        records,
    )


# ============================================================
# MAIN LOOP
# ============================================================

args_global = None


def main():
    global args_global

    enable_ansi()

    parser = argparse.ArgumentParser(
        description=(
            "Crypto Prediction Markets Companion for Robinhood: 15-minute predictions "
            "based only on pre-round history."
        )
    )

    parser.add_argument(
        "--asset",
        choices=sorted(ASSET_CONFIGS),
        default=DEFAULT_ASSET,
        help=f"Asset to analyze. Default: {DEFAULT_ASSET}.",
    )

    parser.add_argument(
        "--context-hours",
        type=float,
        default=DEFAULT_CONTEXT_HOURS,
        help=(
            "Recent historical regime window. "
            f"Default: {DEFAULT_CONTEXT_HOURS} hours."
        ),
    )

    parser.add_argument(
        "--pattern-days",
        type=float,
        default=DEFAULT_PATTERN_DAYS,
        help=(
            "Historical pattern library size. "
            f"Default: {DEFAULT_PATTERN_DAYS} days."
        ),
    )

    parser.add_argument(
        "--backtest-hours",
        type=float,
        default=DEFAULT_BACKTEST_HOURS,
        help=(
            "Recent backtest period. "
            f"Default: {DEFAULT_BACKTEST_HOURS} hours."
        ),
    )

    parser.add_argument(
        "--neighbors",
        type=int,
        default=DEFAULT_K_NEIGHBORS,
        help=(
            "Nearest historical 15-minute states. "
            f"Default: {DEFAULT_K_NEIGHBORS}."
        ),
    )

    parser.add_argument(
        "--simulations",
        type=int,
        default=DEFAULT_SIMULATIONS,
        help=(
            "Bootstrap simulations for live prediction. "
            f"Default: {DEFAULT_SIMULATIONS}."
        ),
    )

    parser.add_argument(
        "--backtest-simulations",
        type=int,
        default=DEFAULT_BACKTEST_SIMULATIONS,
        help=(
            "Bootstrap simulations per backtest round. "
            f"Default: {DEFAULT_BACKTEST_SIMULATIONS}."
        ),
    )

    parser.add_argument(
        "--weight-prior-rounds",
        type=float,
        default=DEFAULT_WEIGHT_PRIOR_ROUNDS,
        help=(
            "Bayesian prior round count used to stabilize adaptive model weights. "
            f"Default: {DEFAULT_WEIGHT_PRIOR_ROUNDS:g}."
        ),
    )

    parser.add_argument(
        "--weight-base-score",
        type=float,
        default=DEFAULT_WEIGHT_BASE_SCORE,
        help=(
            "Minimum raw score retained by every component before normalization. "
            f"Default: {DEFAULT_WEIGHT_BASE_SCORE:g}."
        ),
    )

    parser.add_argument(
        "--live-refresh",
        type=float,
        default=DEFAULT_LIVE_REFRESH_SECONDS,
        help=(
            "Seconds between live current-price screen refreshes. "
            f"Default: {DEFAULT_LIVE_REFRESH_SECONDS}."
        ),
    )

    parser.add_argument(
        "--target-mode",
        choices=["manual", "auto"],
        default=DEFAULT_TARGET_MODE,
        help=(
            "Target source. manual asks for the real Robinhood target "
            "each round; auto uses the benchmark-derived target. "
            f"Default: {DEFAULT_TARGET_MODE}."
        ),
    )

    parser.add_argument(
        "--source",
        choices=[
            "proxy",
        ],
        default="proxy",
        help=(
            "Price source. v9 uses the free multi-exchange BRTI-like proxy."
        ),
    )

    args = parser.parse_args()
    args_global = args

    if args.context_hours < 1:
        print(
            "--context-hours must be at least 1."
        )
        return 1

    if args.pattern_days < 1:
        print(
            "--pattern-days must be at least 1."
        )
        return 1

    if args.neighbors < 5:
        print(
            "--neighbors must be at least 5."
        )
        return 1

    if args.weight_prior_rounds < 0:
        print(
            "--weight-prior-rounds must be >= 0."
        )
        return 1

    if args.weight_base_score <= 0:
        print(
            "--weight-base-score must be > 0."
        )
        return 1

    if args.live_refresh < 0.25:
        print(
            "--live-refresh must be at least 0.25 seconds."
        )
        return 1

    source = BenchmarkSource(
        args.source,
        args.asset,
    )

    stats = load_stats(args.asset)

    print()
    print("=" * 76)
    print(f" ROBINHOOD CRYPTO PREDICTION COMPANION — {args.asset}/USD")
    print("=" * 76)
    print(
        f"Benchmark source: {source.label}"
    )
    print("Independent companion tool — not affiliated with or endorsed by Robinhood.")
    print(
        "Live proxy combines public spot quotes from Coinbase, Kraken, "
        "Bitstamp, Gemini and Crypto.com when available."
    )
    print(
        "Historical proxy merges public 1-minute candles from the exchanges "
        "that provide the requested period."
    )
    print(
        "Prediction is frozen from the round-start historical state."
    )
    print(
        "Every round ALWAYS produces UP or DOWN — there is no NO SIGNAL state."
    )
    print(
        "Model weights are learned automatically from the component "
        "walk-forward backtest scorecard."
    )
    print(
        "Probabilities are TARGET-AWARE: each model estimates whether the "
        "15-minute move can reach the manually entered Robinhood target."
    )
    print(
        f"Adaptive weight prior: {args.weight_prior_rounds:g} rounds | "
        f"base score: {args.weight_base_score:g}"
    )
    print(
        f"Live price display: refresh every {args.live_refresh:g} seconds "
        f"(DISPLAY ONLY — does not change frozen signal)"
    )
    print(
        f"Target mode: {args.target_mode.upper()} "
        f"(the Robinhood target DOES affect the target-aware probability)"
    )
    print(
        "WARNING: proxy prices and proxy settlement are estimates; "
        "Robinhood uses official CME CF BRTI."
    )
    print()

    try:
        (
            candles,
            index,
            records,
        ) = load_model_history(
            args
        )
    except Exception as exc:
        print(
            f"History load failed: {exc}"
        )
        return 1

    # --------------------------------------------------------
    # START WITH CURRENT ROUND
    # --------------------------------------------------------

    now = datetime.now().astimezone()

    active_start, active_end = (
        current_round(
            now
        )
    )

    active_start_ts = int(
        active_start
        .astimezone(
            timezone.utc
        )
        .timestamp()
    )

    try:
        if args.target_mode == "manual":
            active_target = ask_manual_target(
                active_start,
                active_end,
            )
            active_target_quality = "MANUAL Robinhood target"
        else:
            (
                active_target,
                active_target_quality,
            ) = get_or_estimate_target(
                index=index,
                boundary=active_start,
            )

        (
            reconstructed_start_reference,
            reconstructed_start_reference_quality,
        ) = round_start_reference_from_history(
            index=index,
            boundary=active_start,
        )

        active_round = (
            build_round_prediction(
                index=index,
                records=records,
                start=active_start,
                end=active_end,
                target=active_target,
                target_quality=active_target_quality,
                args=args,
                start_reference=reconstructed_start_reference,
                start_reference_quality=reconstructed_start_reference_quality,
            )
        )

    except Exception as exc:
        print(
            f"Could not reconstruct current round: {exc}"
        )
        print(
            "Waiting for the next 15-minute boundary..."
        )

        active_round = None

    current_round_reconstructed = (
        now
        > active_start
        + timedelta(seconds=5)
    )

    if active_round is not None:
        try:
            initial_live_price = source.latest()
        except Exception:
            initial_live_price = None

        print_round(
            round_info=active_round,
            stats=stats,
            source=source,
            reconstructed=current_round_reconstructed,
            current_market_price=initial_live_price,
        )

    # --------------------------------------------------------
    # CONTINUOUS ROUNDS
    # --------------------------------------------------------

    while True:
        try:
            if active_round is None:
                _, next_boundary = (
                    current_round()
                )
            else:
                next_boundary = (
                    active_round[
                        "end"
                    ]
                )

            # During most of the round, continuously show current market
            # price. This is DISPLAY ONLY and never changes the frozen model.
            if active_round is not None:
                monitor_round_until_last_minute(
                    round_info=active_round,
                    stats=stats,
                    source=source,
                    index=index,
                    context_hours=args.context_hours,
                    reconstructed=(
                        active_round["start"]
                        < datetime.now().astimezone()
                        - timedelta(seconds=5)
                    ),
                    refresh_seconds=args.live_refresh,
                )

            # During the final 60 seconds, capture approximately one
            # benchmark reading per second for the settlement/start target.
            captured_value, captured_samples = (
                sample_last_minute_before_boundary(
                    source=source,
                    boundary=next_boundary,
                    show_status=True,
                )
            )

            # Boundary has now arrived.
            # Refresh historical candles so the just-completed minute exists.
            (
                candles,
                index,
                records,
            ) = load_model_history(
                args
            )

            # This boundary benchmark is used to evaluate the OLD round.
            # In manual target mode, the NEW round target will be entered
            # separately by the user.
            (
                boundary_benchmark,
                boundary_quality,
            ) = get_or_estimate_target(
                index=index,
                boundary=next_boundary,
                captured_value=captured_value,
                captured_samples=captured_samples,
            )

            # -----------------------------------------
            # SETTLE OLD ROUND
            # -----------------------------------------

            if active_round is not None:
                if (
                    boundary_benchmark
                    >= active_round[
                        "target"
                    ]
                ):
                    actual = (
                        "UP / ABOVE"
                    )
                else:
                    actual = (
                        "DOWN / BELOW"
                    )

                hit = (
                    actual
                    == active_round[
                        "prediction"
                    ]
                )

                record_result(
                    stats=stats,
                    round_info=active_round,
                    settlement=boundary_benchmark,
                    actual_direction=actual,
                    hit=hit,
                )

                print_settlement(
                    round_info=active_round,
                    settlement=boundary_benchmark,
                    settlement_quality=boundary_quality,
                    actual=actual,
                    hit=hit,
                    stats=stats,
                )

                time.sleep(
                    2
                )

            # -----------------------------------------
            # CREATE NEXT ROUND AT THE SAME BOUNDARY
            # -----------------------------------------

            new_start = (
                next_boundary
            )

            new_end = (
                new_start
                + timedelta(
                    minutes=15
                )
            )

            if args.target_mode == "manual":
                next_target = ask_manual_target(
                    new_start,
                    new_end,
                )
                next_target_quality = "MANUAL Robinhood target"
            else:
                next_target = boundary_benchmark
                next_target_quality = boundary_quality

            active_round = (
                build_round_prediction(
                    index=index,
                    records=records,
                    start=new_start,
                    end=new_end,
                    target=next_target,
                    target_quality=next_target_quality,
                    args=args,
                    start_reference=boundary_benchmark,
                    start_reference_quality=boundary_quality,
                )
            )

            try:
                new_round_live_price = source.latest()
            except Exception:
                new_round_live_price = None

            print_round(
                round_info=active_round,
                stats=stats,
                source=source,
                reconstructed=False,
                current_market_price=new_round_live_price,
            )

        except KeyboardInterrupt:
            print(
                "\nStopped."
            )
            return 0

        except Exception as exc:
            print()
            print(
                f"Runtime error: {exc}"
            )
            print(
                "Retrying in 5 seconds..."
            )
            time.sleep(
                5
            )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
