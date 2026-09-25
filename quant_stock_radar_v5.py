"""台美股量化選股與策略雷達 V4
研究用途，不構成投資建議。

使用方式：
  python quant_stock_radar.py --us-file us_tickers.csv --tw-file tw_tickers.csv
若不提供股票池檔案，會使用內建示範股票池。
"""
from __future__ import annotations

import argparse
import logging
import math
import time
import io
import requests
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class Config:
    period: str = "2y"
    interval: str = "1d"
    ma_short: int = 20
    ma_long: int = 60
    ma_trend: int = 200
    bias_min: float = -3.0
    bias_max: float = 5.0
    pe_max: float = 35.0
    tw_min_avg_volume_lots: int = 3000
    us_min_market_cap: float = 10_000_000_000
    volume_ratio_bonus: float = 1.2
    rsi_period: int = 14
    output_csv: str = "quant_stock_picks.csv"
    all_results_csv: str = "quant_stock_all_results.csv"
    retry_count: int = 3
    retry_wait_seconds: float = 1.5
    request_pause_seconds: float = 0.15
    weights: dict = field(default_factory=lambda: {
        "trend": 25,
        "momentum": 20,
        "valuation": 15,
        "fundamental": 25,
        "volume": 10,
        "entry": 5,
    })

CFG = Config()

# 示範股票池；正式使用建議改用 CSV 維護。
DEFAULT_US = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "ORCL", "AMD", "MU", "PLTR"]
DEFAULT_TW = ["2330.TW", "2317.TW", "2454.TW", "2308.TW", "2382.TW", "3231.TW", "3711.TW", "2881.TW", "2882.TW", "2891.TW"]


# V4：自動擴充股票池來源。若來源暫時不可用，會退回內建示範池。
US_LIST_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
TWSE_LIST_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_LIST_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

def _clean_us_symbol(x: str) -> str:
    return str(x).strip().upper().replace("/", "-")

def auto_us_universe(max_symbols: int = 1200) -> list[str]:
    """抓 NASDAQ Screener 的美股清單。市值門檻仍在正式掃描時再次檢查。"""
    try:
        headers={"User-Agent":"Mozilla/5.0","Accept":"application/json,text/plain,*/*","Referer":"https://www.nasdaq.com/"}
        r=requests.get(US_LIST_URL,headers=headers,timeout=20); r.raise_for_status()
        payload=r.json(); rows=((payload.get("data") or {}).get("rows") or [])
        df=pd.DataFrame(rows)
        if df.empty or "symbol" not in df.columns: raise ValueError("NASDAQ 清單格式異常")
        # API marketCap 通常為字串美元值；先用它做粗篩，缺值者保留到正式掃描。
        mc=pd.to_numeric(df.get("marketCap"),errors="coerce")
        keep=mc.isna() | (mc >= CFG.us_min_market_cap)
        syms=[_clean_us_symbol(x) for x in df.loc[keep,"symbol"].dropna()]
        syms=[x for x in syms if x and not any(ch in x for ch in "^=")]
        return list(dict.fromkeys(syms))[:max_symbols]
    except Exception as exc:
        log.warning("美股自動股票池取得失敗，改用內建池：%s",exc)
        return DEFAULT_US.copy()

def _extract_tw_codes(rows) -> list[str]:
    if not isinstance(rows,list): return []
    out=[]
    for row in rows:
        if not isinstance(row,dict): continue
        code=None
        for k in ("公司代號","SecuritiesCompanyCode","Code","股票代號"):
            if k in row and row[k]: code=str(row[k]).strip(); break
        if code and code.isdigit(): out.append(code)
    return out

def auto_tw_universe(max_symbols: int = 1800) -> list[str]:
    """抓上市＋上櫃公司代號；成交量 3000 張門檻在正式掃描時套用。"""
    try:
        headers={"User-Agent":"Mozilla/5.0"}
        a=requests.get(TWSE_LIST_URL,headers=headers,timeout=20); a.raise_for_status()
        b=requests.get(TPEX_LIST_URL,headers=headers,timeout=20); b.raise_for_status()
        listed=_extract_tw_codes(a.json())
        otc=_extract_tw_codes(b.json())
        syms=[f"{x}.TW" for x in listed] + [f"{x}.TWO" for x in otc]
        syms=list(dict.fromkeys(syms))[:max_symbols]
        if not syms: raise ValueError("台股清單為空")
        return syms
    except Exception as exc:
        log.warning("台股自動股票池取得失敗，改用內建池：%s",exc)
        return DEFAULT_TW.copy()

def get_universe(market: str, mode: str = "auto", max_symbols: int = 1200) -> list[str]:
    if mode == "demo": return DEFAULT_US.copy() if market == "US" else DEFAULT_TW.copy()
    return auto_us_universe(max_symbols) if market == "US" else auto_tw_universe(max_symbols)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("quant-radar")


def safe_num(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else np.nan
    except (TypeError, ValueError):
        return np.nan


def load_tickers(path: Optional[str], market: str) -> list[str]:
    if not path:
        return DEFAULT_US.copy() if market == "US" else DEFAULT_TW.copy()
    df = pd.read_csv(path)
    col = next((c for c in df.columns if c.lower() in {"ticker", "symbol", "code"}), df.columns[0])
    raw = df[col].dropna().astype(str).str.strip().tolist()
    if market == "US":
        return [x.upper() for x in raw]
    out = []
    for x in raw:
        if x.endswith((".TW", ".TWO")):
            out.append(x.upper())
        else:
            # 無法可靠判定上市/上櫃時，先以 .TW 嘗試，失敗會改 .TWO
            out.append(f"{x}.TW")
    return out


def download_history(ticker: str) -> tuple[str, pd.DataFrame]:
    candidates = [ticker]
    if ticker.endswith(".TW"):
        candidates.append(ticker[:-3] + ".TWO")
    elif ticker.endswith(".TWO"):
        candidates.append(ticker[:-4] + ".TW")

    last_error = None
    for candidate in candidates:
        for attempt in range(CFG.retry_count):
            try:
                df = yf.download(candidate, period=CFG.period, interval=CFG.interval,
                                 auto_adjust=True, progress=False, threads=False, timeout=10)
                if isinstance(df.columns, pd.MultiIndex):
                    # 單 ticker 在新版 yfinance 也可能回 MultiIndex
                    try:
                        df = df.xs(candidate, axis=1, level=1)
                    except Exception:
                        df.columns = df.columns.get_level_values(0)
                df = df.dropna(how="all")
                if not df.empty and "Close" in df.columns and len(df) >= CFG.ma_trend + 2:
                    return candidate, df
            except Exception as exc:
                last_error = exc
                time.sleep(CFG.retry_wait_seconds * (attempt + 1))
        log.warning("%s 無有效行情，嘗試下一代號", candidate)
    raise RuntimeError(f"{ticker} 無法取得足夠行情資料: {last_error}")


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def indicators(df: pd.DataFrame) -> dict:
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float) if "Volume" in df else pd.Series(index=df.index, dtype=float)
    ma20 = close.rolling(CFG.ma_short).mean()
    ma60 = close.rolling(CFG.ma_long).mean()
    ma200 = close.rolling(CFG.ma_trend).mean()
    rrsi = rsi(close, CFG.rsi_period)
    avgvol20 = volume.rolling(20).mean()
# MACD (12, 26, 9)
ema12 = close.ewm(span=12, adjust=False).mean()
ema26 = close.ewm(span=26, adjust=False).mean()
macd = ema12 - ema26
macd_signal = macd.ewm(span=9, adjust=False).mean()
macd_hist = macd - macd_signal

# Bollinger Bands (20, 2)
bb_mid = close.rolling(20).mean()
bb_std = close.rolling(20).std()
bb_upper = bb_mid + (2 * bb_std)
bb_lower = bb_mid - (2 * bb_std)

current = close.iloc[-1]
    ret = lambda n: (current / close.iloc[-(n+1)] - 1) * 100 if len(close) > n else np.nan
    return {
        "Latest Price": current,
        "MA20": ma20.iloc[-1], "MA60": ma60.iloc[-1], "MA200": ma200.iloc[-1],
        "Golden Cross": bool(ma20.iloc[-2] <= ma60.iloc[-2] and ma20.iloc[-1] > ma60.iloc[-1]),
        "Above MA200": bool(current > ma200.iloc[-1]),
        "MA60 Above MA200": bool(ma60.iloc[-1] > ma200.iloc[-1]),
        "BIAS20": (current - ma20.iloc[-1]) / ma20.iloc[-1] * 100,
        "RSI14": rrsi.iloc[-1],
      "MACD": macd.iloc[-1],
"MACD Signal": macd_signal.iloc[-1],
"MACD Hist": macd_hist.iloc[-1],
      "BB Upper": bb_upper.iloc[-1],
"BB Mid": bb_mid.iloc[-1],
"BB Lower": bb_lower.iloc[-1],
        "Avg Volume20": avgvol20.iloc[-1],
        "Volume Ratio": volume.iloc[-1] / avgvol20.iloc[-1] if avgvol20.iloc[-1] else np.nan,
        "1M Return": ret(21), "3M Return": ret(63), "6M Return": ret(126),
    }


def get_fundamentals(ticker: str) -> dict:
    """基本面缺值一律回 N/A，不讓單一股票拖垮掃描。"""
    result = {"Company Name": ticker, "Market Cap": np.nan, "P/E": np.nan,
              "Revenue Growth": np.nan, "EPS Growth": np.nan, "ROE": np.nan,
              "Free Cash Flow": np.nan, "Debt To Equity": np.nan}
    for attempt in range(CFG.retry_count):
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            result.update({
                "Company Name": info.get("shortName") or info.get("longName") or ticker,
                "Market Cap": safe_num(info.get("marketCap")),
                "P/E": safe_num(info.get("trailingPE") or info.get("forwardPE")),
                "Revenue Growth": safe_num(info.get("revenueGrowth")),
                "EPS Growth": safe_num(info.get("earningsGrowth")),
                "ROE": safe_num(info.get("returnOnEquity")),
                "Free Cash Flow": safe_num(info.get("freeCashflow")),
                "Debt To Equity": safe_num(info.get("debtToEquity")),
            })
            return result
        except Exception as exc:
            if attempt == CFG.retry_count - 1:
                log.warning("%s 基本面取得失敗: %s", ticker, exc)
            time.sleep(CFG.retry_wait_seconds * (attempt + 1))
    return result


def benchmark_returns(ticker: str) -> dict:
    try:
        _, df = download_history(ticker)
        c = df["Close"].astype(float)
        return {n: (c.iloc[-1] / c.iloc[-(d+1)] - 1) * 100 for n, d in [("1M",21),("3M",63),("6M",126)]}
    except Exception:
        return {"1M": np.nan, "3M": np.nan, "6M": np.nan}


def score_row(row: dict) -> dict:
    # 子分數皆正規化到 0~各自權重，避免黑箱。
    trend = 0.0
    if row["Golden Cross"]: trend += 10
    if row["Above MA200"]: trend += 9
    if row["MA60 Above MA200"]: trend += 6

    momentum = 0.0

# 價格動能：12分
for key, pts in (("3M Return", 4), ("6M Return", 4),
                 ("Relative Strength 3M", 2), ("Relative Strength 6M", 2)):
    if pd.notna(row.get(key)) and row[key] > 0:
        momentum += pts

# MACD：8分
macd = row.get("MACD")
macd_signal = row.get("MACD Signal")
macd_hist = row.get("MACD Hist")

if pd.notna(macd) and pd.notna(macd_signal):
    if macd > macd_signal:
        momentum += 5

if pd.notna(macd_hist) and macd_hist > 0:
    momentum += 3

    pe = row.get("P/E")
    if pd.isna(pe): valuation = 5.0
    elif 0 < pe < 20: valuation = 15.0
    elif pe < CFG.pe_max: valuation = 11.0
    elif pe < 50: valuation = 5.0
    else: valuation = 0.0

    fundamental = 0.0
    for key, pts in (("Revenue Growth",6),("EPS Growth",6),("ROE",5),("Free Cash Flow",3)):
        v = row.get(key)
        if pd.notna(v) and v > 0: fundamental += pts

    vr = row.get("Volume Ratio")
    volume_score = 10.0 if pd.notna(vr) and vr >= CFG.volume_ratio_bonus else (5.0 if pd.notna(vr) and vr >= 0.8 else 0.0)

    # Entry Score：10分
bias = row.get("BIAS20")
rrsi = row.get("RSI14")
price = row.get("Latest Price")
bb_mid = row.get("BB Mid")
bb_lower = row.get("BB Lower")

entry = 0.0

# BIAS20：3分
if pd.notna(bias) and CFG.bias_min <= bias <= CFG.bias_max:
    entry += 3

# RSI：2分
if pd.notna(rrsi) and 30 <= rrsi <= 70:
    entry += 2

# Bollinger Bands：5分
if pd.notna(price) and pd.notna(bb_mid) and pd.notna(bb_lower):
    if bb_lower <= price <= bb_mid:
        entry += 5
    return {"Trend Score": trend, "Momentum Score": momentum, "Valuation Score": valuation,
            "Fundamental Score": fundamental, "Volume Score": volume_score, "Entry Score": entry,
            "Total Score": trend + momentum + valuation + fundamental + volume_score + entry}


def risk_flags(row: dict) -> str:
    flags = []
    if pd.notna(row.get("RSI14")) and row["RSI14"] > 70: flags.append("RSI過熱")
    if pd.notna(row.get("BIAS20")) and row["BIAS20"] > CFG.bias_max: flags.append("乖離偏高")
    if not row.get("Above MA200", False): flags.append("低於MA200")
    if pd.notna(row.get("P/E")) and row["P/E"] >= CFG.pe_max: flags.append("P/E偏高")
    return "、".join(flags) if flags else "-"


def screen_market(tickers: list[str], market: str, benchmarks: dict[str, dict]) -> list[dict]:
    rows = []
    for i, raw in enumerate(tickers, 1):
        log.info("[%s %d/%d] %s", market, i, len(tickers), raw)
        try:
            ticker, hist = download_history(raw)
            ind = indicators(hist)
            fund = get_fundamentals(ticker)
            bench_key = "SPY" if market == "US" else "0050.TW"
            b = benchmarks[bench_key]
            row = {"Market": market, "Ticker": ticker, **ind, **fund}
            row["Relative Strength 1M"] = row["1M Return"] - b["1M"]
            row["Relative Strength 3M"] = row["3M Return"] - b["3M"]
            row["Relative Strength 6M"] = row["6M Return"] - b["6M"]

            # 股票池門檻
            if market == "US" and pd.notna(row["Market Cap"]) and row["Market Cap"] < CFG.us_min_market_cap:
                continue
            if market == "TW" and pd.notna(row["Avg Volume20"]) and row["Avg Volume20"] < CFG.tw_min_avg_volume_lots * 1000:
                continue

            row.update(score_row(row))
            row["Risk Flag"] = risk_flags(row)
            # 核心候選：真正發生黃金交叉 + BIAS合理；PE缺值不硬殺，若有值則需低於門檻
            row["Core Candidate"] = bool(
                row["Golden Cross"] and
                CFG.bias_min <= row["BIAS20"] <= CFG.bias_max and
                (pd.isna(row["P/E"]) or (0 < row["P/E"] < CFG.pe_max))
            )
            rows.append(row)
        except Exception as exc:
            log.error("%s 跳過：%s", raw, exc)
        time.sleep(CFG.request_pause_seconds)
    return rows


def backtest_ticker(ticker: str) -> dict:
    """簡易事件回測：只使用當時可見的價格訊號；基本面歷史點-in-time資料不由 yfinance 穩定提供，因此 V1 不納入歷史基本面。"""
    try:
        _, df = download_history(ticker)
        c = df["Close"].astype(float)
        ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
        bias = (c - ma20) / ma20 * 100
        signal = (ma20.shift(1) <= ma60.shift(1)) & (ma20 > ma60) & bias.between(CFG.bias_min, CFG.bias_max)
        idxs = np.flatnonzero(signal.fillna(False).to_numpy())
        records = []
        for idx in idxs:
            if idx + 60 >= len(c):
                continue
            base = c.iloc[idx]
            rec = {"5D": c.iloc[idx+5]/base-1, "20D": c.iloc[idx+20]/base-1, "60D": c.iloc[idx+60]/base-1}
            # 60日持有期內最大回撤
            path = c.iloc[idx:idx+61] / base
            rec["MaxDD"] = (path / path.cummax() - 1).min()
            records.append(rec)
        if not records:
            return {"Signals":0, "5D Avg":np.nan, "20D Avg":np.nan, "60D Avg":np.nan, "20D WinRate":np.nan, "Avg MaxDD":np.nan, "Sharpe20":np.nan}
        r = pd.DataFrame(records)
        sharpe20 = (r["20D"].mean()/r["20D"].std(ddof=1)*np.sqrt(252/20)) if len(r)>1 and r["20D"].std(ddof=1)>0 else np.nan
        return {"Signals":len(r), "5D Avg":r["5D"].mean(), "20D Avg":r["20D"].mean(), "60D Avg":r["60D"].mean(),
                "20D WinRate":(r["20D"]>0).mean(), "Avg MaxDD":r["MaxDD"].mean(), "Sharpe20":sharpe20}
    except Exception as exc:
        log.warning("%s 回測失敗: %s", ticker, exc)
        return {"Signals":0}


def main():
    parser = argparse.ArgumentParser(description="台美股量化選股與策略雷達 V4")
    parser.add_argument("--us-file", help="美股股票池 CSV，需含 ticker/symbol/code 欄")
    parser.add_argument("--tw-file", help="台股股票池 CSV，需含 ticker/symbol/code 欄")
    parser.add_argument("--backtest-top", type=int, default=10, help="對總分前 N 名執行簡易回測")
    args = parser.parse_args()

    us = load_tickers(args.us_file, "US")
    tw = load_tickers(args.tw_file, "TW")
    log.info("股票池：US=%d, TW=%d", len(us), len(tw))

    benchmarks = {"SPY": benchmark_returns("SPY"), "QQQ": benchmark_returns("QQQ"), "0050.TW": benchmark_returns("0050.TW")}
    rows = screen_market(us, "US", benchmarks) + screen_market(tw, "TW", benchmarks)
    if not rows:
        raise SystemExit("沒有可輸出的結果。請檢查網路、股票池或 yfinance 狀態。")

    df = pd.DataFrame(rows).sort_values("Total Score", ascending=False).reset_index(drop=True)
    df.insert(0, "Rank", np.arange(1, len(df)+1))

    # 對前 N 名加入回測摘要
    bt_cols = ["Signals","5D Avg","20D Avg","60D Avg","20D WinRate","Avg MaxDD","Sharpe20"]
    for c in bt_cols: df[c] = np.nan
    for idx in df.head(args.backtest_top).index:
        bt = backtest_ticker(df.at[idx, "Ticker"])
        for k, v in bt.items():
            if k in df.columns: df.at[idx, k] = v

    df.to_csv(CFG.all_results_csv, index=False, encoding="utf-8-sig")
    picks = df[df["Core Candidate"]].copy()
    picks.to_csv(CFG.output_csv, index=False, encoding="utf-8-sig")

    display_cols = ["Rank","Market","Ticker","Company Name","Latest Price","Total Score","Golden Cross","BIAS20","RSI14","Volume Ratio","P/E","Relative Strength 3M","Risk Flag"]
    print("\n=== 台美股量化雷達：總分前 20 ===")
    print(df[display_cols].head(20).round(2).to_string(index=False))
    print(f"\n核心候選：{len(picks)} 檔")
    print(f"完整結果：{CFG.all_results_csv}")
    print(f"核心候選：{CFG.output_csv}")
    print("\n提醒：分數代表符合本策略規則的程度，不代表未來必然上漲。")

if __name__ == "__main__":
    main()
