# 台美股量化選股雷達 V3

V3 在 V2 掃描器上新增「個股儀表板」：
- 個股價格 + MA20 / MA60 / MA200 圖
- 90 日成交量圖
- 趨勢、動能、估值、基本面、成交量、買點分數視覺化
- 基本面快照
- RSI / BIAS / 相對強弱 / 風險提示
- 排行榜、核心候選、事件回測、CSV 下載

## 本機啟動
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Streamlit Community Cloud
1. 將本資料夾上傳至 GitHub repository。
2. 在 Streamlit Community Cloud 建立 App。
3. Main file path 選 `app.py`。
4. Deploy 後即可用手機瀏覽器開啟。

## 股票池
可上傳 CSV，第一欄或 `ticker` / `symbol` / `code` 欄放股票代號。
台股可只填 `2330`，系統會先嘗試 `.TW`，無資料時再嘗試 `.TWO`。

> 研究工具，不構成投資建議。分數是規則符合度，不代表未來必然上漲。yfinance 資料可能延遲或缺漏。
