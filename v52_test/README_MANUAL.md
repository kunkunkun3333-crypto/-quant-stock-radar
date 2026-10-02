# V5.2 人工部署候選包 — 2026-10-02

這是完整可檢查的程式包，不是已完成真實行情驗收的正式版。尚不具備正式部署條件。
本次未登入、操作或部署 GitHub / Streamlit / Google 帳號。

## 舊檔案處理

在 GitHub main 建立新測試分支 v52-manual-test，之後僅新增整個 v52_test 資料夾。
main 和原 upgrade/v5.2 分支都不寫入。本次新增的分支尚未建立，需由使用者操作。
所有原有檔案保留，包括 app.py、app_v4.py、requirements.txt、原核心程式與工作流程。
沒有要刪除或替換的舊檔案。v52_test 中同名程式是獨立副本，不覆蓋根目錄。
不要上傳 ZIP 本身作為應用程式；後續上傳解壓後的 v52_test 資料夾。
不要建立或合併 Pull Request。不要重設或刪除原正式 App。

## 新測試 App 欄位參考（現在不必操作，待分支與檔案確認後逐步進行）

- Repository: kunkunkun3333-crypto/-quant-stock-radar
- Branch: v52-manual-test
- Main file path: v52_test/app_v52_test.py
- App URL / Custom subdomain: quant-stock-radar-v52-manual-test
- 預計 URL: https://quant-stock-radar-v52-manual-test.streamlit.app/
- 這只是建議網址，尚未註冊或部署；若被占用再另選名稱。
- Advanced settings / Python: 3.12（本次測試版本）
- 不需加入憑證或 Secrets。
- 原 quant-stock-radar-v52-test.streamlit.app 不在本次包更新範圍。

程式入口與 requirements.txt 在同一資料夾，Streamlit 支援這種獨立依賴配置。
官方參考：https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization

## 保留的功能與計算

Quality / Entry / Risk / Trend / 大盤 / 產業 / 排名解釋 / 資料完整度 / 回測 / 限流與快取程式均保留。
Quality 按可用基本面加權，缺資料顯示 N/A 並重分配可用權重。
Entry 包含 RSI、乖離、均線、量比、動能；下降趨勢及超賣有分數上限，大盤不明或偏弱扣分。
Risk 分數越高風險越高，含波動、ATR、回撤、暴漲、異常量、趨勢、估值與資料缺失。
總分初始權重：Quality 30%、Entry 30%、Trend 15%、Industry 10%、Institutional 10%、安全分 5%。安全分是100減Risk；缺失重新加權。
詳細公式與集中設定見 v52_config.py、v52_engine.py，早期完整說明見 historical_reports/V52_ACCEPTANCE.md。

5/20/60交易日回測：訊號日收盤到第N個交易日收盤；各持有期內事件不重疊。
勝率=到期收盤價高於訊號日收盤價的樣本比例，持平仍計入分母。不是期間曾上漲比例。
補充隔日開盤到期收盤扣50bps成本統計。最大回撤是事件持有期間，非投資組合回撤。
至少504筆才切較早70% / 較新30%，跨切分標籤排除；參數固定不依勝率調整。
回測只涵蓋技術＋大盤，沒有用今天基本面冒充過去財報。現存公司池仍有存活偏誤。
完整多因子模型尚未驗證，full_model_validated=False，高信心名單不放行。

## 資料與未解決問題

股票池：TWSE / TPEX 官方上市櫃公司清單；科技模式依官方8類分類，不等於所有AI主題。
行情及可得基本面：Yahoo Finance / yfinance；調整後日線不是即時成交報價。
官方快照：TWSE / TPEX 單日報價與本益比，不能取代還原歷史或混入回測。
法人買賣超、連續買賣天數與Institutional Score：N/A。
月營收YoY、可驗證EPS Growth、財報公開時點：N/A。Yahoo Earnings Growth不冒充EPS Growth。
部分股票P/E、EPS、毛利率等由來源决定是否可得；沒有資料不造數值。

2026-10-01 使用者Cloud截圖：嘗試912、成功0、資料失敗912。
截圖可見多檔皆有2017-02-18、2017-06-03、2025-08-01等日期收盤缺值，示例3/2434筆。
尚未核對官方交易日及原始OHLCV，不能斷言是休市日；目前嚴格拒絕整段受損行情。
本包沒有刪掉缺損日期或補假價格，也沒有解決此歷史缺值阻擋；因此可能仍無排行。
缺值不再觸發全來源限流，但每檔暫停重試5分鐘；真實429冷卻30分鐘。
行情快取6小時、基本面24小時，備援最多7日，過舊標記並限制信心。
120秒是新網路請求預算，不是整體掃描硬性時限；重部署可能遺失本機SQLite快取。
未完成真實10檔指標核對、912檔成功行情效能及新Cloud部署驗收。
歷史合成912檔效能不代表真實下載效能。歷史報告中的登入障礙/未部署狀態只代表當時，不是目前狀態。

## 本次小幅整理

沿用原程式，只隔離到新資料夾、預設示範10檔、顯示組建5.2-manual-20261002。
測試入口改為app_v52_test.py；原10檔UI測試明確選完整測試股票池，以保持原測試涵蓋。
主要依賴固定為本次實際測試版本；無模型權重變更。
完整測試結果见test_results.txt；測試使用合成行情，不代表Yahoo真實行情驗收。

## 後续驗收門檻

先核對Yahoo缺損日期與官方交易日，区分非交易日空列、停牌及真正資料洞，保留處理紀錄。
再完成真實10檔與大盤指標、5/20/60日樣本核對、冷暖快取及完整股票池測試。
完成完整模型樣本外驗證後才討論高信心資格。正式替換V5.1仍需使用者另行確認。

## 開發者本機驗證

cd v52_test
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m streamlit run app_v52_test.py
