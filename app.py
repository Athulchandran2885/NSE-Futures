"""
Nifty 100 Stock Futures M1/M2 Premium Scanner — Streamlit app
----------------------------------------------------------------
Two data modes:
  1. Live fetch  - app tries to pull NSE's official bhavcopy files itself.
                   Works great locally; may get blocked on cloud hosting
                   (NSE often 403s datacenter IP ranges).
  2. Manual load - you download the bhavcopy ZIPs yourself in a browser
                   (always works, since it's just you clicking a link) and
                   upload them here. Use this if live fetch fails on
                   Streamlit Cloud.

Deploy: push this folder to a GitHub repo, then create the app at
https://share.streamlit.io pointing at app.py. See README.md for the
full walkthrough.
"""

import io
import zipfile
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Nifty 100 Futures Scanner", layout="wide")

NSE_HOME = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
NIFTY100_URL = "https://niftyindices.com/IndexConstituent/ind_nifty100list.csv"


def fo_bhavcopy_url(date: datetime) -> str:
    return (
        "https://nsearchives.nseindia.com/content/fo/"
        f"BhavCopy_NSE_FO_0_0_0_{date:%Y%m%d}_F_0000.csv.zip"
    )


def cm_bhavcopy_url(date: datetime) -> str:
    return (
        "https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{date:%Y%m%d}_F_0000.csv.zip"
    )


@st.cache_data(show_spinner=False, ttl=3600)
def load_nifty100() -> pd.DataFrame:
    r = requests.get(NIFTY100_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content))
    df.columns = [c.strip() for c in df.columns]
    return df


def fetch_zip_csv_live(url: str) -> pd.DataFrame:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(NSE_HOME, timeout=15)  # warm up cookies
    r = s.get(url, timeout=30)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        with z.open(z.namelist()[0]) as f:
            return pd.read_csv(f)


def read_zip_csv_upload(uploaded_file) -> pd.DataFrame:
    with zipfile.ZipFile(uploaded_file) as z:
        with z.open(z.namelist()[0]) as f:
            return pd.read_csv(f)


def run_scanner(cm: pd.DataFrame, fo: pd.DataFrame, n100_symbols: set, date: datetime):
    cm.columns = [c.strip() for c in cm.columns]
    fo.columns = [c.strip() for c in fo.columns]

    spot = cm[cm["TckrSymb"].isin(n100_symbols)][["TckrSymb", "ClsPric"]]
    spot = spot.rename(columns={"TckrSymb": "Symbol", "ClsPric": "Spot"})

    futstk = fo[fo["FinInstrmTp"].isin(["STF", "FUTSTK"])]
    futstk = futstk[futstk["TckrSymb"].isin(n100_symbols)].copy()
    futstk["XpryDt"] = pd.to_datetime(futstk["XpryDt"])
    futstk = futstk.sort_values(["TckrSymb", "XpryDt"])
    futstk["rank"] = futstk.groupby("TckrSymb").cumcount() + 1

    rows, excluded = [], []
    for sym in sorted(n100_symbols):
        sub = futstk[futstk["TckrSymb"] == sym]
        sp = spot[spot["Symbol"] == sym]
        if sub.empty:
            excluded.append((sym, "No FUTSTK contract in FO bhavcopy"))
            continue
        if sp.empty:
            excluded.append((sym, "No matching CM bhavcopy row"))
            continue

        spot_px = float(sp["Spot"].iloc[0])
        m1 = sub[sub["rank"] == 1]
        m2 = sub[sub["rank"] == 2]
        if m1.empty:
            excluded.append((sym, "M1 contract missing"))
            continue

        m1_px = float(m1["ClsPric"].iloc[0])
        m1_exp = m1["XpryDt"].iloc[0]

        row = {
            "Symbol": sym,
            "Spot": spot_px,
            "M1_Future": m1_px,
            "M1_Expiry": m1_exp.date(),
        }

        if not m2.empty:
            m2_px = float(m2["ClsPric"].iloc[0])
            m2_exp = m2["XpryDt"].iloc[0]
            spread_abs = m2_px - m1_px
            spread_pct = (spread_abs / m1_px) * 100
            row.update({
                "M2_Future": m2_px,
                "M2_Expiry": m2_exp.date(),
                "M2_minus_M1_Rs": round(spread_abs, 2),
                "M2_minus_M1_Pct": round(spread_pct, 2),
            })
        else:
            row.update({
                "M2_Future": "N/A", "M2_Expiry": "N/A",
                "M2_minus_M1_Rs": "N/A", "M2_minus_M1_Pct": "N/A",
            })

        rows.append(row)

    cols = ["Symbol", "Spot", "M1_Future", "M1_Expiry", "M2_Future", "M2_Expiry",
            "M2_minus_M1_Rs", "M2_minus_M1_Pct"]
    out = pd.DataFrame(rows, columns=cols)
    numeric_spread = pd.to_numeric(out["M2_minus_M1_Pct"], errors="coerce")
    out = out.assign(_sort=numeric_spread).sort_values("_sort", ascending=False).drop(columns="_sort").reset_index(drop=True)
    exc = pd.DataFrame(excluded, columns=["Symbol", "Reason"])
    return out, exc


# ---------------- UI ----------------
st.title("Nifty 100 Stock Futures — M1/M2 Premium Scanner")
st.caption("Source: NSE official bhavcopy (CM + FO segments) and niftyindices.com constituent list.")

mode = st.radio("Data source", ["Live fetch from NSE", "Upload bhavcopy files manually"], horizontal=True)
target_date = st.date_input("Bhavcopy trading date", value=datetime.now() - timedelta(days=1))
target = datetime.combine(target_date, datetime.min.time())

cm_df = fo_df = None

if mode == "Live fetch from NSE":
    if st.button("Fetch & Run"):
        try:
            with st.spinner("Fetching CM bhavcopy..."):
                cm_df = fetch_zip_csv_live(cm_bhavcopy_url(target))
            with st.spinner("Fetching FO bhavcopy..."):
                fo_df = fetch_zip_csv_live(fo_bhavcopy_url(target))
        except requests.HTTPError as e:
            st.error(
                f"Live fetch failed ({e}). This is usually NSE blocking the "
                "hosting IP, or the chosen date being a non-trading day. "
                "Switch to 'Upload bhavcopy files manually' below as a workaround."
            )
else:
    st.markdown(
        "Download these two files yourself in a browser (they're public NSE links), "
        "then upload the ZIPs here:\n\n"
        f"- CM bhavcopy: `{cm_bhavcopy_url(target)}`\n"
        f"- FO bhavcopy: `{fo_bhavcopy_url(target)}`"
    )
    cm_upload = st.file_uploader("Upload CM bhavcopy .zip", type="zip", key="cm")
    fo_upload = st.file_uploader("Upload FO bhavcopy .zip", type="zip", key="fo")
    if cm_upload and fo_upload and st.button("Run scanner on uploaded files"):
        cm_df = read_zip_csv_upload(cm_upload)
        fo_df = read_zip_csv_upload(fo_upload)

if cm_df is not None and fo_df is not None:
    try:
        n100 = load_nifty100()
        n100_symbols = set(n100["Symbol"].str.strip())
        out, exc = run_scanner(cm_df, fo_df, n100_symbols, target)

        st.success(f"Checked {len(n100_symbols)} Nifty 100 constituents — {len(out)} F&O-eligible with data, {len(exc)} excluded.")

        tab1, tab2, tab3 = st.tabs(["Full table", "Top 10 by M2-M1 spread %", "Excluded stocks"])
        with tab1:
            st.dataframe(out, use_container_width=True)
            st.download_button("Download full CSV", out.to_csv(index=False), "nifty100_scanner.csv")
        with tab2:
            top10 = out.head(10).set_index("Symbol")["M2_minus_M1_Pct"]
            st.bar_chart(top10)
        with tab3:
            st.dataframe(exc, use_container_width=True)
    except Exception as e:
        st.error(f"Processing error: {e}")
