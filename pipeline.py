"""
Financial Data Analysis Pipeline: Tesla (TSLA) & GameStop (GME)
---------------------------------------------------------------
Extracts, transforms, and loads historical stock price data and quarterly
revenue data into clean Star-Schema tables ready for Power BI consumption.

Outputs:
  - data/raw/         : Raw API and scraped cache files
  - data/processed/   : Star-schema relational CSV tables:
      * dim_company.csv
      * dim_date.csv
      * fact_daily_stock.csv
      * fact_quarterly_revenue.csv
"""

import os
import sys
import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
import yfinance as yf

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_RAW_DIR = os.path.join(BASE_DIR, "data", "raw")
DATA_PROCESSED_DIR = os.path.join(BASE_DIR, "data", "processed")

# Company metadata
COMPANIES = {
    "TSLA": {
        "name": "Tesla, Inc.",
        "sector": "Consumer Cyclical",
        "industry": "Auto Manufacturers",
        "ipo_date": "2010-06-29",
        "sec_cik": "0001318605",
        "ibm_url": "https://cf-courses-data.s3.us.cloud-object-storage.appdomain.cloud/IBMDeveloperSkillsNetwork-PY0220EN-SkillsNetwork/labs/project/revenue.htm"
    },
    "GME": {
        "name": "GameStop Corp.",
        "sector": "Consumer Cyclical",
        "industry": "Specialty Retail",
        "ipo_date": "2002-02-13",
        "sec_cik": "0001326380",
        "ibm_url": "https://cf-courses-data.s3.us.cloud-object-storage.appdomain.cloud/IBMDeveloperSkillsNetwork-PY0220EN-SkillsNetwork/labs/project/stock.html"
    }
}

SEC_HEADERS = {"User-Agent": "FinancialDataProject admin@financialdataproject.org"}


def ensure_directories():
    """Ensure raw and processed data directories exist."""
    os.makedirs(DATA_RAW_DIR, exist_ok=True)
    os.makedirs(DATA_PROCESSED_DIR, exist_ok=True)


# ----------------------------------------------------------------------
# 1. STOCK DATA EXTRACTION & SPLIT ADJUSTMENT
# ----------------------------------------------------------------------

def extract_and_process_stock(ticker: str) -> pd.DataFrame:
    """
    Downloads historical stock prices via yfinance, computes split factors,
    unadjusted close prices, and daily return percentages.
    """
    print(f"[{ticker}] Extracting historical daily stock prices...")
    t = yf.Ticker(ticker)
    df = t.history(period="max", auto_adjust=False)

    if df.empty:
        raise ValueError(f"No stock data returned for {ticker}")

    # Reset index and remove timezone for clean Power BI ingestion
    df = df.reset_index()
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)

    # Cache raw stock data
    raw_path = os.path.join(DATA_RAW_DIR, f"{ticker.lower()}_stock_raw.csv")
    df.to_csv(raw_path, index=False)
    print(f"[{ticker}] Cached raw data to {raw_path} ({len(df)} rows)")

    # Sort chronologically
    df = df.sort_values("Date").reset_index(drop=True)

    # Standardize column names
    # In yfinance, 'Close' is split-adjusted in modern feeds, 'Stock Splits' tracks split events
    splits = df["Stock Splits"].values
    n = len(splits)
    cumulative_factors = np.ones(n, dtype=float)

    # Calculate backwards cumulative split multiplier for nominal unadjusted prices
    current_factor = 1.0
    for i in range(n - 1, -1, -1):
        cumulative_factors[i] = current_factor
        if splits[i] > 0:
            current_factor *= splits[i]

    df["Company_Key"] = ticker
    df["Date_Str"] = df["Date"].dt.strftime("%Y-%m-%d")
    df["Date_Key"] = df["Date"].dt.strftime("%Y%m%d").astype(int)
    df["Cumulative_Split_Factor"] = cumulative_factors
    
    # Split-adjusted prices (standard for continuous financial return analysis)
    df["Adj_Close"] = df["Close"].round(2)
    df["Open"] = df["Open"].round(2)
    df["High"] = df["High"].round(2)
    df["Low"] = df["Low"].round(2)
    
    # Reconstructed historical nominal price before splits
    df["Unadjusted_Close"] = (df["Adj_Close"] * df["Cumulative_Split_Factor"]).round(2)
    
    # Daily percentage return on adjusted close: (P_t - P_{t-1}) / P_{t-1}
    df["Daily_Return_Pct"] = df["Adj_Close"].pct_change().round(6).fillna(0.0)

    # Volume as integer
    df["Volume"] = df["Volume"].fillna(0).astype("int64")

    # Standardize column names
    df["Stock_Splits"] = df["Stock Splits"].fillna(0.0)

    # Select and order final fact columns
    cols = [
        "Company_Key",
        "Date_Str",
        "Date_Key",
        "Open",
        "High",
        "Low",
        "Close",
        "Adj_Close",
        "Unadjusted_Close",
        "Volume",
        "Daily_Return_Pct",
        "Stock_Splits"
    ]
    fact_df = df[cols].rename(columns={"Date_Str": "Date", "Close": "Close_Split_Adjusted"})
    return fact_df


# ----------------------------------------------------------------------
# 2. QUARTERLY REVENUE EXTRACTION & NORMALIZATION
# ----------------------------------------------------------------------

def extract_ibm_historical_revenue(url: str, ticker: str) -> pd.DataFrame:
    """Extracts legacy pre-2022 quarterly revenue from static IBM HTML mirror."""
    records = []
    try:
        resp = requests.get(url, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        target_table = None
        for table in soup.find_all("table"):
            if "Quarterly Revenue" in table.text:
                target_table = table
                break
        if target_table:
            tbody = target_table.find("tbody") or target_table
            for tr in tbody.find_all("tr"):
                cols = tr.find_all("td")
                if len(cols) >= 2:
                    d_str = cols[0].text.strip()
                    rev_str = cols[1].text.strip().replace("$", "").replace(",", "")
                    if d_str and rev_str:
                        try:
                            val = float(rev_str)
                            records.append({
                                "Date": pd.to_datetime(d_str).strftime("%Y-%m-%d"),
                                "Revenue_Millions": val,
                                "Source": "IBM_Historical",
                                "Rank": 1
                            })
                        except ValueError:
                            pass
    except Exception as e:
        print(f"[{ticker}] Warning: IBM extraction failed: {e}")
    return pd.DataFrame(records)


def extract_sec_edgar_revenue(cik: str, ticker: str) -> pd.DataFrame:
    """Extracts official 10-Q and 10-K derived quarterly revenue from SEC EDGAR API."""
    records = []
    try:
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
        resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if resp.status_code == 200:
            gaap = resp.json().get("facts", {}).get("us-gaap", {})
            candidates = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"]
            chosen = next((c for c in candidates if c in gaap and "USD" in gaap[c].get("units", {})), None)
            
            if chosen:
                items = gaap[chosen]["units"]["USD"]
                df = pd.DataFrame(items)
                df["start"] = pd.to_datetime(df["start"])
                df["end"] = pd.to_datetime(df["end"])
                df["days"] = (df["end"] - df["start"]).dt.days
                
                # Quarters: durations between 80 and 105 days
                q_df = df[(df["days"] >= 80) & (df["days"] <= 105)].copy()
                for _, row in q_df.iterrows():
                    d_str = row["end"].strftime("%Y-%m-%d")
                    val = round(row["val"] / 1e6, 2)
                    records.append({
                        "Date": d_str,
                        "Revenue_Millions": val,
                        "Source": "SEC_10Q",
                        "Rank": 3
                    })

                # Derive Q4 from 10-K (full year minus Q1, Q2, Q3)
                k_df = df[(df["days"] >= 350) & (df["days"] <= 375) & (df["form"] == "10-K")].drop_duplicates(subset=["end", "val"])
                for _, krow in k_df.iterrows():
                    fy_end = krow["end"]
                    fy_start = krow["start"]
                    fy_val = round(krow["val"] / 1e6, 2)
                    
                    interim_qs = q_df[(q_df["end"] > fy_start) & (q_df["end"] < fy_end - pd.Timedelta(days=45))].drop_duplicates(subset=["end"], keep="last")
                    if len(interim_qs) == 3:
                        q4_val = round(fy_val - (interim_qs["val"].sum() / 1e6), 2)
                        records.append({
                            "Date": fy_end.strftime("%Y-%m-%d"),
                            "Revenue_Millions": q4_val,
                            "Source": "SEC_10K_Derived_Q4",
                            "Rank": 3
                        })
    except Exception as e:
        print(f"[{ticker}] Warning: SEC EDGAR extraction failed: {e}")
    return pd.DataFrame(records)


def extract_yfinance_recent_revenue(ticker: str) -> pd.DataFrame:
    """Extracts recent quarterly revenue from yfinance quarterly income statement."""
    records = []
    try:
        t = yf.Ticker(ticker)
        inc = t.quarterly_income_stmt
        if inc is not None and "Total Revenue" in inc.index:
            for dt, val in inc.loc["Total Revenue"].dropna().items():
                d_str = pd.to_datetime(dt).strftime("%Y-%m-%d")
                records.append({
                    "Date": d_str,
                    "Revenue_Millions": round(val / 1e6, 2),
                    "Source": "yfinance",
                    "Rank": 2
                })
    except Exception as e:
        print(f"[{ticker}] Warning: yfinance quarterly revenue failed: {e}")
    return pd.DataFrame(records)


def extract_and_process_quarterly_revenue(ticker: str, cik: str, ibm_url: str) -> pd.DataFrame:
    """
    Harmonizes quarterly revenue across SEC EDGAR, yfinance, and historical IBM data,
    deduplicates near-adjacent dates, and computes YoY/QoQ growth.
    """
    print(f"[{ticker}] Extracting quarterly revenue from SEC EDGAR, yfinance, and historical sources...")
    df_ibm = extract_ibm_historical_revenue(ibm_url, ticker)
    df_sec = extract_sec_edgar_revenue(cik, ticker)
    df_yf = extract_yfinance_recent_revenue(ticker)

    combined = pd.concat([df_ibm, df_yf, df_sec], ignore_index=True)
    if combined.empty:
        raise ValueError(f"Could not retrieve revenue data for {ticker}")

    # Cache raw revenue
    raw_path = os.path.join(DATA_RAW_DIR, f"{ticker.lower()}_revenue_raw.csv")
    combined.to_csv(raw_path, index=False)
    print(f"[{ticker}] Cached raw revenue data to {raw_path}")

    # Sort by date and source priority (SEC > yfinance > IBM)
    combined = combined.sort_values(["Date", "Rank"]).reset_index(drop=True)

    # Filter near-duplicate quarters (e.g., within 50 days of each other)
    filtered = []
    for _, row in combined.iterrows():
        if not filtered:
            filtered.append(row.to_dict())
        else:
            prev = filtered[-1]
            days_diff = (pd.to_datetime(row["Date"]) - pd.to_datetime(prev["Date"])).days
            if days_diff < 50:
                # Replace with higher rank entry
                if row["Rank"] >= prev["Rank"]:
                    filtered[-1] = row.to_dict()
            else:
                filtered.append(row.to_dict())

    clean_df = pd.DataFrame(filtered).sort_values("Date").reset_index(drop=True)

    # Date calculations
    date_dt = pd.to_datetime(clean_df["Date"])
    clean_df["Quarter_End_Date"] = clean_df["Date"]
    clean_df["Date_Key"] = date_dt.dt.strftime("%Y%m%d").astype(int)
    clean_df["Company_Key"] = ticker
    clean_df["Fiscal_Year"] = date_dt.dt.year
    clean_df["Fiscal_Quarter"] = "Q" + date_dt.dt.quarter.astype(str)
    clean_df["Fiscal_Period"] = clean_df["Fiscal_Year"].astype(str) + "-" + clean_df["Fiscal_Quarter"]

    # Compute Growth rates
    clean_df["YoY_Growth_Pct"] = clean_df["Revenue_Millions"].pct_change(4).round(4)
    clean_df["QoQ_Growth_Pct"] = clean_df["Revenue_Millions"].pct_change(1).round(4)

    cols = [
        "Company_Key",
        "Quarter_End_Date",
        "Date_Key",
        "Fiscal_Year",
        "Fiscal_Quarter",
        "Fiscal_Period",
        "Revenue_Millions",
        "YoY_Growth_Pct",
        "QoQ_Growth_Pct",
        "Source"
    ]
    return clean_df[cols]


# ----------------------------------------------------------------------
# 3. DIMENSION TABLES GENERATION
# ----------------------------------------------------------------------

def generate_dim_company() -> pd.DataFrame:
    """Generates the Dim_Company reference table."""
    rows = []
    for ticker, info in COMPANIES.items():
        rows.append({
            "Company_Key": ticker,
            "Ticker": ticker,
            "Company_Name": info["name"],
            "Sector": info["sector"],
            "Industry": info["industry"],
            "IPO_Date": info["ipo_date"]
        })
    return pd.DataFrame(rows)


def generate_dim_date(min_date: str, max_date: str, trading_dates: set) -> pd.DataFrame:
    """Generates a complete daily date dimension table from min_date to max_date."""
    dates = pd.date_range(start=min_date, end=max_date, freq="D")
    df = pd.DataFrame({"Date_DT": dates})

    df["Date"] = df["Date_DT"].dt.strftime("%Y-%m-%d")
    df["Date_Key"] = df["Date_DT"].dt.strftime("%Y%m%d").astype(int)
    df["Year"] = df["Date_DT"].dt.year
    df["Quarter"] = "Q" + df["Date_DT"].dt.quarter.astype(str)
    df["Year_Quarter"] = df["Year"].astype(str) + "-" + df["Quarter"]
    df["Month"] = df["Date_DT"].dt.month
    df["Month_Name"] = df["Date_DT"].dt.strftime("%B")
    df["Month_Short"] = df["Date_DT"].dt.strftime("%b")
    df["Day_Of_Week"] = df["Date_DT"].dt.dayofweek + 1  # 1=Monday, 7=Sunday
    df["Day_Name"] = df["Date_DT"].dt.strftime("%A")
    df["Is_Weekend"] = df["Day_Of_Week"].isin([6, 7]).astype(int)
    df["Is_Trading_Day"] = df["Date"].isin(trading_dates).astype(int)

    cols = [
        "Date_Key",
        "Date",
        "Year",
        "Quarter",
        "Year_Quarter",
        "Month",
        "Month_Name",
        "Month_Short",
        "Day_Of_Week",
        "Day_Name",
        "Is_Weekend",
        "Is_Trading_Day"
    ]
    return df[cols]


# ----------------------------------------------------------------------
# 4. MAIN ORCHESTRATOR
# ----------------------------------------------------------------------

def run_pipeline():
    """Executes the full Phase 1 ETL pipeline and exports star-schema CSVs."""
    print("=" * 60)
    print("Starting Financial Data Analysis Pipeline (Phase 1)")
    print("=" * 60)
    ensure_directories()

    # 1. Process Stock Data
    stock_dfs = []
    all_trading_dates = set()
    for ticker in COMPANIES:
        sdf = extract_and_process_stock(ticker)
        stock_dfs.append(sdf)
        all_trading_dates.update(sdf["Date"].tolist())

    fact_daily_stock = pd.concat(stock_dfs, ignore_index=True)
    fact_daily_stock = fact_daily_stock.sort_values(["Company_Key", "Date"]).reset_index(drop=True)

    # 2. Process Quarterly Revenue
    rev_dfs = []
    for ticker, info in COMPANIES.items():
        rdf = extract_and_process_quarterly_revenue(ticker, info["sec_cik"], info["ibm_url"])
        rev_dfs.append(rdf)

    fact_quarterly_revenue = pd.concat(rev_dfs, ignore_index=True)
    fact_quarterly_revenue = fact_quarterly_revenue.sort_values(["Company_Key", "Quarter_End_Date"]).reset_index(drop=True)

    # 3. Generate Dimensions
    dim_company = generate_dim_company()

    min_stock_date = fact_daily_stock["Date"].min()
    max_stock_date = fact_daily_stock["Date"].max()
    max_rev_date = fact_quarterly_revenue["Quarter_End_Date"].max()
    overall_max_date = max(max_stock_date, max_rev_date)

    dim_date = generate_dim_date(min_stock_date, overall_max_date, all_trading_dates)

    # 4. Export Processed Star-Schema Tables
    outputs = {
        "dim_company.csv": dim_company,
        "dim_date.csv": dim_date,
        "fact_daily_stock.csv": fact_daily_stock,
        "fact_quarterly_revenue.csv": fact_quarterly_revenue
    }

    print("\n" + "=" * 60)
    print("Exporting Processed Datasets (Star Schema)")
    print("=" * 60)

    for filename, df in outputs.items():
        out_path = os.path.join(DATA_PROCESSED_DIR, filename)
        df.to_csv(out_path, index=False)
        print(f"-> {filename:<28} | Rows: {len(df):>6} | Cols: {len(df.columns):>2} | Path: {out_path}")

    print("\nPipeline execution completed successfully!")
    return outputs


if __name__ == "__main__":
    run_pipeline()
