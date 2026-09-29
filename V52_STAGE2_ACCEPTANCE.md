# V5.2 第二階段驗收：候選程式完成，部署受權限阻擋

記錄時間：2026-09-29（台北）。**尚未具備正式部署条件。沒有變更GitHub main或V5.1正式站。**

## GitHub與部署

- GitHub外掛已連線，帳號 `kunkunkun3333-crypto`。
- repository metadata顯示帳號有admin/push權限；這不代表外掛存取權也相同。
- 真正呼叫Create Branch建立 `upgrade/v5.2` 時，GitHub回傳403 `Resource not accessible by integration`。分支**沒有在遠端建立**。安裝清單查詢回傳空清單。
- 本地 `upgrade/v5.2` 程式及提交完整保留。未重試其他寫入介面，未修改main，未建立PR或合併。
- Streamlit Community Cloud瀏覽器可到達登入頁，但未登入。GitHub外掛登入不等於Streamlit登入。
- **測試站網址：尚無，不能宣稱已部署。**
- 需先讓GitHub連線具備此repository的Contents讀寫權限，再建立測試分支；之後完成Streamlit登入、建立獨立app。指定branch=`upgrade/v5.2`，main file=`app_v52_test.py`。不可選main或改現有V5.1 app設定。

## 本輪修改

- 新增 `v52_cache.py`：SQLite壓縮JSON成功快照、每股票與資料期間獨立鍵、程序內請求互斥、來源級冷卻、單一股票錯誤記錄。
- 更新 `v52_data.py`：明確捕捉單檔Yahoo錯誤；單檔失敗不影響其他快取；不再因連續批次失敗跳過後面的已快取股票。每檔掃描紀錄保存該檔失敗原因。
- 更新 `v52_config.py`：行情6小時、基本面24小時、最長7日取得年齡備援、每輪網路預算120秒。120秒是停止新增請求的預算，不是整輪硬性超時；正在執行的請求與計算可能超出。
- 來源限流後冷卻30分鐘；一般連續3次錯誤後冷卻5分鐘；每個失敗鍵5分鐘負向快取。不同股票池／頁面重掃沿用逐檔成功資料。手動更新只標記需重抓，不清除舊快照或限流冷卻。
- 多個Streamlit session共用同程序鎖，避免同時下載相同股票；未實作跨多個OS程序的分散式鎖。
- 調整後歷史失敗時最多沿用7日內舊快照，UI及逐檔紀錄标記備援；備援時不列高信心。資料已過時仍不是即時報價。
- 更新 `requirements.txt`：yfinance從0.2.66升至固定1.7.0，停用套件內部暫時性錯誤重試，未使用代理輪換、IP切換或其他限流繞過方式。
- 新增 `v52_official.py` 與官方快照查詢按鈕：TWSE／TPEX全市場收盤及本益比單日快照，與Yahoo還原歷史完全分開，不補造RSI、回測或EPS。部分端點不穩定時該來源N/A。
- 新增 `app_v52_test.py`：醒目測試站標示，供獨立Streamlit app使用。
- 新增 `tests/test_cache.py` 及 `scripts/validate_v52.py`：可重現快取與效能測试，合成行情只存在臨時測試資料庫，未寫進正式cache。
- 更新 `.github/workflows/main.yml`：測試分支push觸發本地同等測試。此檔尚未發布，所以GitHub Actions尚未實際運行。

## 測試結果

| 項目 | 結果 | 範圍／限制 |
|---|---|---|
| V5.2自動化測試 | 29項通過 | 包括評分、時序回測、UI操作、快取、負向快取、限流、重啟持久化 |
| V5.1回歸 | 5項通過 | 原版程式沒有改動；新依賴環境亦通過測試 |
| 官方股票池 | 1,988檔／科技912檔 | 實際讀取TWSE/TPEX清單 |
| 10檔Yahoo日線 | 不通過 | 更新套件後仍YFRateLimitError；無真實分數可驗證 |
| 912檔真實股票池故障處理 | 通過保護邏輯，資料驗收失敗 | 0.258秒記錄912檔資料缺失；冷卻期間0次Yahoo載入呼叫；沒有任何真實歷史成功 |
| 912檔合成資料warm-cache計算 | 912/912，55.055秒 | 每檔2,520日，無網路，單次本地程序量測，非Cloud實測 |
| 合成負載尖峰RSS | 347.8MB | 包括該程序資料與計算，未含Cloud平台額外負擔／多使用者 |
| 合成快取大小 | 64.96MB | 1,824個鍵（912行情＋912基本面）；實際歷史在記憶體162.69MB |
| 合成分數 | 26.97–77.22 | 所查Total/Entry/Risk有限值，不是任何真實公司的排名 |
| Streamlit Cloud部署／效能 | 未完成 | GitHub建立分支403，Streamlit尚未登入 |

實際抽查股票：2330、2317、2454、2408、2344、2337、6488、8299、2881、2412；這只是待驗股票名單，不代表取得其歷史資料。

本輪限流實測還發現早期快取草稿的SQLite交易未提交問題，導致冷卻未保存，已修正並新增跨Store實例、重啟後冷卻測試。修復後保留已觀察的限流狀態，再做912檔冷卻驗收；没有把修復前14次載入呼叫隱藏成一次。

## 官方備援實際狀態

最新一輪：TWSE本益比回傳1,081筆，來源日期2026-09-24；TWSE收盤及TPEX收盤連線逾時、TPEX本益比回傳HTTP520。可取得的本益比只在官方快照表呈現，未混入Yahoo品質分數。前一輪曾成功取得其他端點，不代表本輪仍成功；本報告以此輪結果為準。

## 仍為N/A或未驗收

- Yahoo未取得日線時：RSI、Bias、量比、Entry、Risk、Trend、產業動能、歷史勝率、大盤環境皆無可驗證真實值。
- Yahoo基本面未取得時Quality／來源PE／EPS等N/A；官方單日PE有另列來源與日期。
- 法人籌碼、月營收YoY、可靠EPS成長、財報當時公開日期仍未接入。
- 全多因子歷史驗證未完成；高信心資格依然不放行。技術事件統計不可冒充完整模型勝率。
- 本機磁碟快取可在同磁碟程序重啟後續用，但Cloud重部署可能清掉磁碟，不是永久儲存。沒有跨主機共享cache。
- 912檔首次成功下載耗時、Cloud記憶體、多使用者負載仍待驗證；本地55秒不能當Cloud服務承諾。

## 可重現命令

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/validate_v52.py --mode synthetic --limit 912 --out validation/synthetic_repeat.json
python scripts/validate_v52.py --mode live --limit 10 --out validation/live_ten.json
python scripts/validate_v52.py --mode live --limit 912 --out validation/live_pool.json
streamlit run app_v52_test.py
```

應遵守來源冷卻，切勿刪除冷卻資料庫反覆重試。下一步先修復GitHub外掛對此repository的實際寫入授權，再部署測試分支與登入Streamlit。取得成功行情後才可繼續真實計算核對及Cloud驗收。**在使用者明確確認前不得合併main或替換V5.1。**
