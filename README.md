# Nifty50 / Bank Nifty — EMA8 & VWAP Tracker

Streamlit dashboard that pulls **live intraday candles from the Upstox API v2**,
computes **EMA(8)** and the **session VWAP** for the top-weighted constituents
of NIFTY 50 and BANK NIFTY, and shows:

- Two colour-coded tables (Nifty50 / Bank Nifty) — stock, weight %, price,
  EMA8, VWAP, and a **BULLISH** (price above both EMA8 & VWAP) /
  **BEARISH** (price below both) / **NEUTRAL** tag.
- A TradingView-style candlestick chart (price + EMA8 line + VWAP line +
  volume) for any stock you pick from a dropdown.
- Console/log-style table printouts (visible in the terminal running
  `streamlit run`, or in the Streamlit Cloud "Manage app" logs).

Index composition used (edit the `NIFTY50_TOP10` / `BANKNIFTY_TOP10` dicts
at the top of `app.py` if your weights change):

| NIFTY50 top 10 | Weight % | | BANKNIFTY top 10 | Weight % |
|---|---|---|---|---|
| HDFC Bank | 11.18 | | HDFC Bank | 19.30 |
| ICICI Bank | 9.01 | | ICICI Bank | 14.16 |
| Reliance | 8.00 | | SBI | 10.02 |
| Bharti Airtel | 5.15 | | Axis Bank | 9.59 |
| L&T | 4.44 | | Kotak Bank | 9.31 |
| SBI | 3.88 | | Federal Bank | 6.67 |
| Axis Bank | 3.54 | | IndusInd Bank | 4.99 |
| Infosys | 3.21 | | AU Bank | 4.64 |
| Kotak Bank | 2.64 | | IDFC First Bank | 4.36 |
| ITC | 2.53 | | Bank of Baroda | 4.00 |
| **Total** | **53.58%** | | **Total** | **87.04%** |

---

## 1. Get an Upstox API key & access token

1. Sign up as a developer at https://developer.upstox.com/ and create an app
   (you'll get an **API key** and **API secret**).
2. Upstox access tokens are issued via an **OAuth2 login flow** and are only
   valid until end-of-day (they expire every night — this is an Upstox
   platform limitation, not something this app can work around):
   - Redirect the user to:
     `https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=<API_KEY>&redirect_uri=<REDIRECT_URI>`
   - Upstox redirects back with a `code` query param.
   - Exchange it for an access token:
     ```bash
     curl -X POST https://api.upstox.com/v2/login/authorization/token \
       -d "code=<CODE>" \
       -d "client_id=<API_KEY>" \
       -d "client_secret=<API_SECRET>" \
       -d "redirect_uri=<REDIRECT_URI>" \
       -d "grant_type=authorization_code"
     ```
   - The response contains `access_token` — that's what this app needs.
3. You'll need to repeat step 2 each trading day (or automate it with a
   scheduled script) since Upstox tokens expire daily.

## 2. Run locally

```bash
git clone <your-repo-url>
cd nifty-tracker
pip install -r requirements.txt
export UPSTOX_ACCESS_TOKEN="paste-your-token-here"
streamlit run app.py
```

Alternatively, skip the env var and paste the token directly into the
sidebar text box when the app opens.

## 3. Push to GitHub

```bash
git init
git add app.py requirements.txt README.md .gitignore
git commit -m "Nifty50/BankNifty EMA8-VWAP tracker"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

**Never commit your access token.** Keep it in an environment variable or
Streamlit secrets only.

## 4. Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click **New app**, select your repo/branch, and set the main file to
   `app.py`.
3. Before (or after) deploying, open **Settings → Secrets** and add:
   ```toml
   UPSTOX_ACCESS_TOKEN = "paste-your-token-here"
   ```
4. Deploy. Because the token expires daily, you'll need to update this
   secret each trading morning (or paste a fresh token into the sidebar
   for a one-off session).

## Notes / limitations

- **Live vs. previous session:** the app first tries today's live intraday
  candles for each stock. If the market is closed or no trades have printed
  yet today, it automatically falls back to the most recent previous
  trading day's candles (via Upstox's historical-candle endpoint) and
  labels that stock's row/chart as `PREVIOUS SESSION (YYYY-MM-DD)` instead
  of `LIVE`, so you always see the last available EMA8/VWAP read even when
  the exchange is shut. The previous-day lookup skips weekends but does
  **not** know NSE holidays, so on a holiday it tries up to 5 prior
  weekdays until it finds data.
- **One-by-one loading:** each table fills in row-by-row as every stock is
  fetched (with a progress bar), rather than blocking until all 10 stocks
  finish, so you can see results arrive live.
- The Upstox intraday candle endpoint only returns data for the **current
  trading session** — outside market hours you'll see the previous-session
  fallback kick in as described above.
- EMA(8) and VWAP are both computed from 1-minute (or 30-minute, if you
  switch the interval dropdown) intraday candles, reset each session —
  this matches how VWAP is conventionally defined (cumulative from the
  session open).
- The NSE instrument master (symbol → Upstox `instrument_key` mapping) is
  downloaded once per day from Upstox's `NSE.json.gz` file (their CSV
  instrument files are deprecated) and cached; if a symbol changes trading
  name on the exchange, update the ticker in `NIFTY50_TOP10` /
  `BANKNIFTY_TOP10`.
- Upstox API rate limits apply — the app caches each stock's intraday
  candle data for 60 seconds and previous-session data for 5 minutes to
  stay well within normal limits.
