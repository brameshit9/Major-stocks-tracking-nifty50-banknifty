"""
Nifty50 / Bank Nifty  —  EMA8 & VWAP Momentum Tracker
------------------------------------------------------
Streamlit dashboard that:
  1. Pulls intraday candle data from the Upstox API v2.
  2. Computes EMA(8) and session VWAP for the top-weighted constituents
     of NIFTY 50 and BANK NIFTY (as supplied by the user).
  3. Flags each stock BULLISH  -> last price above BOTH EMA8 and VWAP
             flags each stock BEARISH  -> last price below BOTH EMA8 and VWAP
             else NEUTRAL       -> mixed signal
  4. Renders two colour-coded tables (Nifty50 / Bank Nifty) and a
     TradingView-style candlestick chart (price + EMA8 + VWAP + volume)
     for any stock the user selects.

------------------------------------------------------------------
SETUP
------------------------------------------------------------------
1. Create an app at https://developer.upstox.com/ and generate an
   ACCESS TOKEN (Upstox access tokens are valid for the current
   trading day only — you need to regenerate it daily via the
   OAuth login flow, see README.md).

2. Provide the token to this app in ONE of these ways:
     a) Environment variable:  export UPSTOX_ACCESS_TOKEN="xxxx"
     b) Streamlit secrets (secrets.toml):
            UPSTOX_ACCESS_TOKEN = "xxxx"
     c) Paste it into the sidebar text box at runtime.

3. Run locally:
       pip install -r requirements.txt
       streamlit run app.py

4. Deploy on Streamlit Community Cloud:
       - Push this folder to a GitHub repo.
       - On share.streamlit.io -> "New app" -> point to app.py.
       - In "Secrets" add UPSTOX_ACCESS_TOKEN = "xxxx".
------------------------------------------------------------------
"""

import os
import gzip
import io
import json
from datetime import datetime, timedelta

import requests
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

# ======================================================================
# CONFIG
# ======================================================================
st.set_page_config(page_title="Nifty50 / BankNifty EMA8-VWAP Tracker", layout="wide")

UPSTOX_BASE = "https://api.upstox.com/v2"
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

EMA_SPAN = 8
DEFAULT_INTERVAL = "1minute"   # fixed candle timeframe — no longer user-selectable

# --- User-supplied index composition (top weighted constituents) -----
NIFTY50_TOP10 = {
    "HDFCBANK":   10.27,
    "ICICIBANK":   9.22,
    "RELIANCE":    7.92,
    "BHARTIARTL":  5.37,
    "LT":          4.13,
    "SBIN":        3.81,
    "AXISBANK":    3.16,
    "INFY":        3.55,
    "KOTAKBANK":   2.58,
    "BAJFINANCE":  2.74,
    "M&M":         2.72,
    "ITC":         2.53,
    "ETERNAL":     1.96,
    "SUNPHARMA":   1.89,
    "TITAN":       1.8,
    "HINDUNILVR":  1.67,
     "MARUTI":     1.66,
     "NTPC":       1.48,
     "TATASTEEL":  1.41,
    "SHRIRAMFIN":  1.31,
     "HINDALCO":   1.26,
    "ULTRACEMCO":  1.26,
     "BEL":        1.24,
    "POWERGRID":   1.15,
    "BAJAJ-AUTO"   1.14,
    "ADANIPORTS":  1.12,
    "ASIANPAINT":  1.11,
    "INDIGO":      1.05,
    "GRASIM":      1.05,
    }  # ~82% of NIFTY50 weight

BANKNIFTY_TOP10 = {
    "HDFCBANK":    19.30,
    "ICICIBANK":   14.16,
    "SBIN":        10.02,
    "AXISBANK":     9.59,
    "KOTAKBANK":    9.31,
    "FEDERALBNK":   6.67,
    "INDUSINDBK":   4.99,
    "AUBANK":       4.64,
    "IDFCFIRSTB":   4.36,
    "BANKBARODA":   4.00,
}  # ~87.04% of BANKNIFTY weight


# ======================================================================
# AUTH
# ======================================================================
def get_access_token() -> str:
    token = st.session_state.get("access_token_override", "")
    if token:
        return token
    if "UPSTOX_ACCESS_TOKEN" in st.secrets:
        return st.secrets["UPSTOX_ACCESS_TOKEN"]
    return os.environ.get("UPSTOX_ACCESS_TOKEN", "")


def auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


# ======================================================================
# INSTRUMENT MASTER  (maps trading symbol -> Upstox instrument_key)
#
# Upstox deprecated the CSV instrument files; the JSON files are now the
# recommended source. Each record looks like:
#   {
#     "segment": "NSE_EQ", "name": "JOCIL LIMITED", "exchange": "NSE",
#     "isin": "INE839G01010", "instrument_type": "EQ",
#     "instrument_key": "NSE_EQ|INE839G01010", "lot_size": 1,
#     "trading_symbol": "JOCIL", "short_name": "JOCIL", ...
#   }
# Docs: https://upstox.com/developer/api-documentation/instruments
# ======================================================================
@st.cache_data(ttl=24 * 3600, show_spinner="Loading NSE instrument master...")
def load_instrument_master() -> pd.DataFrame:
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
    resp.raise_for_status()

    # `requests` sometimes auto-decompresses gzip content already (if the
    # server sent Content-Encoding: gzip), so only gzip-decompress if the
    # bytes actually look gzip-compressed (magic number 1f 8b).
    content = resp.content
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)

    data = None
    try:
        data = json.loads(content)
    except Exception:
        pass

    if data is None:
        raise RuntimeError("Could not parse instrument master response as JSON.")

    # The file is normally a flat JSON array of records. Handle the case
    # where it might instead be wrapped in an object (e.g. {"data": [...]})
    # just in case Upstox changes the envelope in future.
    if isinstance(data, dict):
        for key in ("data", "instruments", "results"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break

    df = pd.DataFrame(data)
    if df.empty:
        return pd.DataFrame(columns=["instrument_key", "trading_symbol"])

    required = {"segment", "instrument_type", "instrument_key", "trading_symbol"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Instrument master is missing expected columns {sorted(missing)}. "
            f"Columns actually present: {list(df.columns)}. "
            "Upstox may have changed the file format — check "
            "https://upstox.com/developer/api-documentation/instruments"
        )

    df = df[(df["instrument_type"] == "EQ") & (df["segment"] == "NSE_EQ")]
    return df[["instrument_key", "trading_symbol"]].drop_duplicates()


def symbol_to_instrument_key(symbol: str, master: pd.DataFrame) -> str | None:
    row = master[master["trading_symbol"] == symbol]
    if row.empty:
        return None
    return row.iloc[0]["instrument_key"]


# ======================================================================
# DATA FETCH
# ======================================================================
@st.cache_data(ttl=60, show_spinner=False)
def fetch_intraday_candles(instrument_key: str, token: str, interval: str = DEFAULT_INTERVAL) -> pd.DataFrame:
    """Returns ascending-time OHLCV dataframe for the CURRENT session (today), if any."""
    url = f"{UPSTOX_BASE}/historical-candle/intraday/{instrument_key}/{interval}"
    resp = requests.get(url, headers=auth_headers(token), timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    candles = payload.get("data", {}).get("candles", [])
    return _candles_to_df(candles)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_historical_candles(instrument_key: str, token: str, interval: str, day_str: str) -> pd.DataFrame:
    """Returns ascending-time OHLCV dataframe for a single past calendar date."""
    url = f"{UPSTOX_BASE}/historical-candle/{instrument_key}/{interval}/{day_str}/{day_str}"
    resp = requests.get(url, headers=auth_headers(token), timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    candles = payload.get("data", {}).get("candles", [])
    return _candles_to_df(candles)


def _candles_to_df(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _previous_calendar_weekday(from_date, days_back: int):
    """Return `from_date` minus `days_back` weekdays (Mon-Fri), skipping Sat/Sun.
    This is a simple approximation — it does not know about NSE trading
    holidays, only weekends."""
    d = from_date
    steps = 0
    while steps < days_back:
        d = d - timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            steps += 1
    return d


def get_candles_with_session(instrument_key: str, token: str, interval: str, max_days_back: int = 5):
    """
    Try today's live intraday candles first. If empty (market closed /
    no trades yet / holiday), fall back to the most recent previous
    trading day's candles via the historical-candle endpoint.

    Returns (dataframe, session_label) where session_label is one of:
      "LIVE", "PREVIOUS SESSION (YYYY-MM-DD)", or "NO DATA"
    """
    live = fetch_intraday_candles(instrument_key, token, interval)
    if not live.empty:
        return live, "LIVE"

    today = datetime.now().date()
    for back in range(1, max_days_back + 1):
        day = _previous_calendar_weekday(today, back)
        day_str = day.strftime("%Y-%m-%d")
        hist = fetch_historical_candles(instrument_key, token, interval, day_str)
        if not hist.empty:
            return hist, f"PREVIOUS SESSION ({day_str})"

    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"]), "NO DATA"


# ======================================================================
# INDICATORS
# ======================================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["ema8"] = df["close"].ewm(span=EMA_SPAN, adjust=False).mean()
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    df["vwap"] = (typical_price * df["volume"]).cumsum() / cum_vol
    df["vwap"] = df["vwap"].bfill()
    return df


def classify(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"price": None, "ema8": None, "vwap": None, "signal": "NO DATA"}
    last = df.iloc[-1]
    price, ema8, vwap = last["close"], last["ema8"], last["vwap"]
    if price > ema8 and price > vwap:
        signal = "BULLISH"
    elif price < ema8 and price < vwap:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"
    return {"price": price, "ema8": ema8, "vwap": vwap, "signal": signal}


# ======================================================================
# BUILD SUMMARY TABLE FOR AN INDEX (fetches + renders one stock at a time)
# ======================================================================
def build_summary_progressive(weights: dict, master: pd.DataFrame, token: str,
                               interval: str, table_placeholder, status_placeholder,
                               progress_bar) -> pd.DataFrame:
    rows = []
    symbols = list(weights.items())
    total = len(symbols)

    for i, (symbol, weight) in enumerate(symbols, start=1):
        status_placeholder.write(f"Fetching **{symbol}** ({i}/{total})...")
        progress_bar.progress(i / total)

        inst_key = symbol_to_instrument_key(symbol, master)
        if inst_key is None:
            rows.append({"Stock": symbol, "Weight %": weight, "Price": None, "EMA8": None,
                         "VWAP": None, "Session": "-", "Signal": "SYMBOL NOT FOUND"})
        else:
            try:
                candles, session = get_candles_with_session(inst_key, token, interval)
                candles = add_indicators(candles)
                info = classify(candles)
                rows.append({
                    "Stock": symbol,
                    "Weight %": weight,
                    "Price": round(info["price"], 2) if info["price"] is not None else None,
                    "EMA8": round(info["ema8"], 2) if info["ema8"] is not None else None,
                    "VWAP": round(info["vwap"], 2) if info["vwap"] is not None else None,
                    "Session": session,
                    "Signal": info["signal"],
                })
            except Exception as exc:  # noqa: BLE001
                rows.append({"Stock": symbol, "Weight %": weight, "Price": None, "EMA8": None,
                             "VWAP": None, "Session": "-", "Signal": f"ERROR: {exc}"})

        # Re-render the table after every stock so results appear one by one.
        partial = pd.DataFrame(rows).sort_values("Weight %", ascending=False).reset_index(drop=True)
        table_placeholder.dataframe(
            style_table(partial),
            use_container_width=True, hide_index=True,
        )

    status_placeholder.empty()
    progress_bar.empty()
    out = pd.DataFrame(rows).sort_values("Weight %", ascending=False).reset_index(drop=True)
    print(f"\n=== Summary ({total} stocks) ===")
    print(out.to_string(index=False))
    return out


def style_signal(val: str) -> str:
    if val == "BULLISH":
        return "background-color: #d4f7d4; color: #0a6b0a; font-weight: 600;"
    if val == "BEARISH":
        return "background-color: #fddede; color: #a30000; font-weight: 600;"
    if val == "NEUTRAL":
        return "background-color: #fff6d5; color: #806000; font-weight: 600;"
    return "background-color: #eeeeee; color: #555555;"


def _fmt2(val) -> str:
    """Format a number to exactly 2 decimals; pass through non-numeric / missing values."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "-"
    try:
        return f"{float(val):.2f}"
    except (TypeError, ValueError):
        return str(val)


def style_table(df: pd.DataFrame):
    numeric_cols = [c for c in ["Weight %", "Price", "EMA8", "VWAP"] if c in df.columns]
    fmt = {c: _fmt2 for c in numeric_cols}
    return df.style.format(fmt).map(style_signal, subset=["Signal"])


def render_table_header(title: str, weights: dict):
    st.subheader(title)
    total_weight = sum(weights.values())
    st.caption(f"Constituents to track: {len(weights)}  |  Combined weight: {total_weight:.2f}%")


# ======================================================================
# TRADINGVIEW-STYLE CHART
# ======================================================================
def render_chart(symbol: str, df: pd.DataFrame, session_label: str = ""):
    title_suffix = f" — {session_label}" if session_label else ""
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.75, 0.25], vertical_spacing=0.03,
    )

    fig.add_trace(go.Candlestick(
        x=df["timestamp"], open=df["open"], high=df["high"],
        low=df["low"], close=df["close"], name=symbol,
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["ema8"], mode="lines", name="EMA8",
        line=dict(color="red", width=1.5),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df["timestamp"], y=df["vwap"], mode="lines", name="VWAP",
        line=dict(color="blue", width=1.5),
    ), row=1, col=1)

    colors = np.where(df["close"] >= df["open"], "#26a69a", "#ef5350")
    fig.add_trace(go.Bar(
        x=df["timestamp"], y=df["volume"], name="Volume", marker_color=colors,
    ), row=2, col=1)

    fig.update_layout(
        title=f"{symbol} — Price / EMA8 / VWAP{title_suffix}",
        xaxis_rangeslider_visible=False,
        height=650,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=10, r=10, t=60, b=10),
    )
    st.plotly_chart(fig, use_container_width=True, key=f"chart_{symbol}_{session_label}")


# ======================================================================
# MAIN APP
# ======================================================================
def main():
    st.title("📊 Nifty50 / Bank Nifty — EMA8 & VWAP Tracker")
    st.caption("Data source: Upstox API v2 (intraday candles). Bullish = price above EMA8 & VWAP. "
               "Bearish = price below EMA8 & VWAP.")

    with st.sidebar:
        st.header("Settings")
        token_input = st.text_input(
            "Upstox Access Token (optional override)",
            type="password",
            help="Leave blank to use UPSTOX_ACCESS_TOKEN from env var / st.secrets.",
        )
        if token_input:
            st.session_state["access_token_override"] = token_input

        interval = DEFAULT_INTERVAL  # fixed at 1minute — not user-selectable
        st.caption(f"Candle timeframe: **{interval}** (fixed)")

        refresh_choice = st.selectbox(
            "Auto-refresh every",
            ["Off", "1 min", "2 min", "3 min", "4 min", "5 min"],
            index=0,
            help="Automatically re-fetches and re-renders the whole page on this interval.",
        )
        if refresh_choice != "Off":
            minutes = int(refresh_choice.split()[0])
            if HAS_AUTOREFRESH:
                st_autorefresh(interval=minutes * 60 * 1000, key="auto_refresh_timer")
            else:
                st.warning(
                    "Auto-refresh needs the `streamlit-autorefresh` package "
                    "(add it to requirements.txt and redeploy)."
                )

        if st.button("🔄 Refresh data now"):
            st.cache_data.clear()

        st.markdown("---")
        st.caption("Access tokens issued by Upstox expire at end of trading day. "
                   "Regenerate daily via the OAuth login flow — see README.md.")

    token = get_access_token()
    if not token:
        st.warning("No Upstox access token found. Enter one in the sidebar, or set "
                   "UPSTOX_ACCESS_TOKEN as an environment variable / Streamlit secret.")
        st.stop()

    try:
        master = load_instrument_master()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Failed to load NSE instrument master: {exc}")
        st.stop()

    # ------------------------------------------------------------------
    # 1) NIFTY 50
    # ------------------------------------------------------------------
    render_table_header("NIFTY 50 — Top Weighted Constituents", NIFTY50_TOP10)
    status_ph = st.empty()
    progress_ph = st.progress(0)
    table_ph = st.empty()
    nifty_df = build_summary_progressive(
        NIFTY50_TOP10, master, token, interval, table_ph, status_ph, progress_ph
    )
    bullish = nifty_df[nifty_df["Signal"] == "BULLISH"]
    bearish = nifty_df[nifty_df["Signal"] == "BEARISH"]
    c1, c2 = st.columns(2)
    c1.metric("Bullish weight %", f"{bullish['Weight %'].sum():.2f}%")
    c2.metric("Bearish weight %", f"{bearish['Weight %'].sum():.2f}%")

    st.divider()

    # ------------------------------------------------------------------
    # 2) BANK NIFTY
    # ------------------------------------------------------------------
    render_table_header("BANK NIFTY — Top Weighted Constituents", BANKNIFTY_TOP10)
    status_ph = st.empty()
    progress_ph = st.progress(0)
    table_ph = st.empty()
    bn_df = build_summary_progressive(
        BANKNIFTY_TOP10, master, token, interval, table_ph, status_ph, progress_ph
    )
    bullish = bn_df[bn_df["Signal"] == "BULLISH"]
    bearish = bn_df[bn_df["Signal"] == "BEARISH"]
    c1, c2 = st.columns(2)
    c1.metric("Bullish weight %", f"{bullish['Weight %'].sum():.2f}%")
    c2.metric("Bearish weight %", f"{bearish['Weight %'].sum():.2f}%")

    st.divider()

    # ------------------------------------------------------------------
    # 3) CHARTS — every stock, no dropdown
    # ------------------------------------------------------------------
    st.subheader("Charts — All Stocks")
    all_symbols = sorted(set(list(NIFTY50_TOP10.keys()) + list(BANKNIFTY_TOP10.keys())))
    chart_status_ph = st.empty()
    chart_progress_ph = st.progress(0)
    total_charts = len(all_symbols)

    for i, symbol in enumerate(all_symbols, start=1):
        chart_status_ph.write(f"Charting **{symbol}** ({i}/{total_charts})...")
        chart_progress_ph.progress(i / total_charts)

        inst_key = symbol_to_instrument_key(symbol, master)
        if inst_key is None:
            st.error(f"Instrument key not found for {symbol}.")
            continue

        candles, session = get_candles_with_session(inst_key, token, interval)
        candles = add_indicators(candles)
        if candles.empty:
            st.info(f"{symbol}: no candle data available (market closed and no recent "
                    f"previous-session data found either — try a different interval).")
            continue

        badge = "🟢 LIVE" if session == "LIVE" else f"🟡 {session}"
        st.caption(f"{symbol} — Showing: {badge}")
        render_chart(symbol, candles, session)

    chart_status_ph.empty()
    chart_progress_ph.empty()

    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
