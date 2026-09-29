# Quant Stock Radar V5.2

V5.2 – Quant Decision System。這是待真實行情驗收的研究版候選，正式V5.1尚未替換。

## 本機啟動

```bash
pip install -r requirements.txt
streamlit run app_v52_test.py
```

測試：`python -m unittest discover -s tests -v`。

## 操作

1. 側邊欄先選「示範台股10檔」，按「開始掃描」。
2. 看成功／失敗數及逐檔紀錄；行情缺失時不會產生假分數。
3. 首頁看大盤與排行；個股頁選公司看分項與排名原因。
4. 歷史驗證頁可按需計算；勝率附N，僅是技術訊號事件統計。
5. 法人、月營收、可驗證EPS成長尚未接入，顯示N/A。
6. 確认10檔真實資料與效能後，再測912檔科技池。

Risk越高越危險；其他分數不是勝率。總分是未完成完整模型驗證的實驗權重，高信心把關目前不放行。

## 部署

不要直接覆蓋main。先使用測試分支與獨立Streamlit測試app，測試站入口`app_v52_test.py`。本次Yahoo限流，尚未完成真實行情與Cloud驗收。

原V5.1可用`streamlit run app_v51.py`。原核心`quant_stock_radar_v5.py`及`v51_support.py`保持原樣。

最新第二階段狀態見 [V52_STAGE2_ACCEPTANCE.md](V52_STAGE2_ACCEPTANCE.md)：GitHub外掛建立分支回傳403，Streamlit尚未登入，真實行情仍待驗收。

首次候選版的完整公式、限制、測試、來源、驗收與回復流程見[V52_ACCEPTANCE.md](V52_ACCEPTANCE.md)。
