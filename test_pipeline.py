"""
Test suite for Phase 1 Financial Data Pipeline
----------------------------------------------
Validates data integrity, schemas, relationships, stock split logic,
and date continuities for all generated star-schema tables.
"""

import os
import unittest
import pandas as pd

DATA_PROCESSED_DIR = os.path.join(os.path.dirname(__file__), "data", "processed")


class TestFinancialPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dim_company = pd.read_csv(os.path.join(DATA_PROCESSED_DIR, "dim_company.csv"))
        cls.dim_date = pd.read_csv(os.path.join(DATA_PROCESSED_DIR, "dim_date.csv"))
        cls.fact_daily_stock = pd.read_csv(os.path.join(DATA_PROCESSED_DIR, "fact_daily_stock.csv"))
        cls.fact_quarterly_revenue = pd.read_csv(os.path.join(DATA_PROCESSED_DIR, "fact_quarterly_revenue.csv"))

    def test_files_exist(self):
        """Verify all required files exist and have non-zero size."""
        expected_files = [
            "dim_company.csv",
            "dim_date.csv",
            "fact_daily_stock.csv",
            "fact_quarterly_revenue.csv"
        ]
        for filename in expected_files:
            filepath = os.path.join(DATA_PROCESSED_DIR, filename)
            self.assertTrue(os.path.exists(filepath), f"Missing expected file: {filename}")
            self.assertGreater(os.path.getsize(filepath), 0, f"File is empty: {filename}")

    def test_dim_company(self):
        """Verify Dim_Company schema and values."""
        df = self.dim_company
        expected_cols = ["Company_Key", "Ticker", "Company_Name", "Sector", "Industry", "IPO_Date"]
        self.assertEqual(list(df.columns), expected_cols)
        self.assertEqual(set(df["Company_Key"]), {"TSLA", "GME"})
        self.assertEqual(len(df), 2)
        self.assertTrue(df["Company_Key"].is_unique)

    def test_dim_date(self):
        """Verify Dim_Date completeness and calendar continuity."""
        df = self.dim_date
        expected_cols = [
            "Date_Key", "Date", "Year", "Quarter", "Year_Quarter",
            "Month", "Month_Name", "Month_Short", "Day_Of_Week",
            "Day_Name", "Is_Weekend", "Is_Trading_Day"
        ]
        self.assertEqual(list(df.columns), expected_cols)
        self.assertTrue(df["Date"].is_unique)
        self.assertTrue(df["Date_Key"].is_unique)

        dates = pd.to_datetime(df["Date"])
        date_diffs = dates.diff().dropna()
        self.assertTrue((date_diffs == pd.Timedelta(days=1)).all(), "Dim_Date contains gaps in daily sequence")

    def test_fact_daily_stock(self):
        """Verify Fact_Daily_Stock schema, ranges, splits, and return calculations."""
        df = self.fact_daily_stock
        expected_cols = [
            "Company_Key", "Date", "Date_Key", "Open", "High", "Low",
            "Close_Split_Adjusted", "Adj_Close", "Unadjusted_Close",
            "Volume", "Daily_Return_Pct", "Stock_Splits"
        ]
        self.assertEqual(list(df.columns), expected_cols)
        self.assertEqual(set(df["Company_Key"]), {"TSLA", "GME"})
        self.assertEqual(df["Date"].isna().sum(), 0)
        self.assertEqual(df["Adj_Close"].isna().sum(), 0)
        self.assertTrue((df["Adj_Close"] > 0).all(), "Stock prices must be strictly positive")

        # Verify latest date reaches 2026
        max_date = df["Date"].max()
        self.assertTrue(max_date.startswith("2026"), f"Expected max date in 2026, got {max_date}")

        # Verify stock splits are present
        tsla_splits = df[(df["Company_Key"] == "TSLA") & (df["Stock_Splits"] > 0)]
        self.assertGreaterEqual(len(tsla_splits), 2, "Expected at least 2 TSLA stock splits (5:1 and 3:1)")

        gme_splits = df[(df["Company_Key"] == "GME") & (df["Stock_Splits"] > 0)]
        self.assertGreaterEqual(len(gme_splits), 2, "Expected at least 2 GME stock splits")

        # Verify TSLA unadjusted close vs split-adjusted close prior to 2020 split
        tsla_row = df[(df["Company_Key"] == "TSLA") & (df["Date"] == "2020-08-28")].iloc[0]
        ratio = tsla_row["Unadjusted_Close"] / tsla_row["Adj_Close"]
        self.assertAlmostEqual(ratio, 15.0, delta=0.1, msg=f"Expected 15x cumulative split factor on 2020-08-28 for TSLA, got {ratio}")

    def test_fact_quarterly_revenue(self):
        """Verify Fact_Quarterly_Revenue values, coverage, and growth calculations."""
        df = self.fact_quarterly_revenue
        expected_cols = [
            "Company_Key", "Quarter_End_Date", "Date_Key", "Fiscal_Year",
            "Fiscal_Quarter", "Fiscal_Period", "Revenue_Millions",
            "YoY_Growth_Pct", "QoQ_Growth_Pct", "Source"
        ]
        self.assertEqual(list(df.columns), expected_cols)
        self.assertEqual(set(df["Company_Key"]), {"TSLA", "GME"})
        self.assertTrue((df["Revenue_Millions"] > 0).all(), "Revenue must be positive")

        # Verify coverage into 2026
        self.assertTrue(df["Quarter_End_Date"].max().startswith("2026"))

        # Verify no duplicate quarters within 50 days for each company
        for ticker in ["TSLA", "GME"]:
            comp_df = df[df["Company_Key"] == ticker].sort_values("Quarter_End_Date")
            dates = pd.to_datetime(comp_df["Quarter_End_Date"])
            diffs = dates.diff().dropna().dt.days
            self.assertTrue((diffs >= 50).all(), f"Found duplicate quarters within 50 days for {ticker}")

        # Verify row counts show full historical span
        tsla_q = df[df["Company_Key"] == "TSLA"]
        gme_q = df[df["Company_Key"] == "GME"]
        self.assertGreaterEqual(len(tsla_q), 60, f"Expected at least 60 quarters for TSLA, got {len(tsla_q)}")
        self.assertGreaterEqual(len(gme_q), 80, f"Expected at least 80 quarters for GME, got {len(gme_q)}")

    def test_referential_integrity(self):
        """Verify foreign keys in fact tables exist in dimension tables."""
        companies = set(self.dim_company["Company_Key"])
        dates = set(self.dim_date["Date"])

        # Check fact_daily_stock
        self.assertTrue(set(self.fact_daily_stock["Company_Key"]).issubset(companies))
        self.assertTrue(set(self.fact_daily_stock["Date"]).issubset(dates))

        # Check fact_quarterly_revenue
        self.assertTrue(set(self.fact_quarterly_revenue["Company_Key"]).issubset(companies))
        self.assertTrue(set(self.fact_quarterly_revenue["Quarter_End_Date"]).issubset(dates))


if __name__ == "__main__":
    unittest.main()
