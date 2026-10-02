# V5.2 授權完成後驗收（2026-09-29 台北）

本紀錄取代 Stage2 的授權阻擋狀態；Stage2 保留作歷史紀錄。

- Connector installation 166163629，selected repositories 僅列出本專案。
- 已成功建立遠端 upgrade/v5.2，實際驗證分支寫入權限。
- main 基準仍為 5d18083fc71641cb2112d33cdb5706d4ce43291b；未修改、合併或變動正式 Streamlit app。
- 本候選提交供獨立測試站使用：branch=upgrade/v5.2，entry=app_v52_test.py。
- Streamlit Cloud 目前停在登入與服務條款頁，尚無測試站網址，也沒有 Cloud 效能結果。

## 本輪實測

- 補齊環境依賴後 V5.2 29 項測試通過；原 V5.1 5 項回歸通過。
- Streamlit 1.64.0、本機 Python 3.12；独立啟動 app_v52_test.py，health 回傳 ok。Cloud 使用不同環境時仍需驗收。
- app_v51.py 與 main 原 app_v4.py 的 SHA256 完全相同。
- 真實官方股票池 1,988 檔、科技股 912 檔。
- 10 檔測試：Yahoo benchmark 首次載入遇 YFRateLimitError；觸發 30 分鐘冷卻。1 次資料載入器呼叫，不代表僅 1 次底層 HTTP 請求；10 檔成功 0，驗收失敗。
- 912 檔再掃：0.219 秒、尖峰 RSS 149.41MB、0 次新 Yahoo 載入呼叫、成功 0/912。這是冷卻與錯誤處理測試，並非行情取得或完整計算效能通過。
- 詳細本轮 JSON：validation/v52_live_ten_authorized.json、validation/v52_live_pool_authorized.json。
- 前輪合成資料 912 檔 warm-cache 55.055 秒、347.8MB 僅為本機合成負載參考，本輪未重跑，不能當作 Cloud 或真實行情效能。

## N/A 與部署結論

法人籌碼、月營收 YoY、可靠 EPS 成長、財報當時公布日期未接入。Yahoo 日線受限時技術指標、風險、趨勢、產業強弱及回測結果沒有真實可驗證值；基本面亦視來源取得狀態顯示 N/A。官方單日快照不冒充 Yahoo 歷史資料。

完整多因子模型未歷史驗證，full_model_validated=False，高信心資格不放行。尚未具備正式部署 V5.2 條件。需先完成獨立 Streamlit 登入與部署、真實 10 檔及全池驗收、Cloud 資源與快取重啟驗證。只有使用者明確確認後才可討論合併 main。
