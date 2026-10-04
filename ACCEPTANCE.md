# 驗證紀錄

日期：2026-10-04（Asia/Taipei）  
環境：Windows、Python 3.12.10；Node 24.19.0 僅用於 JavaScript 語法檢查，程式執行不需要 Node。

## 最小後端驗收

在美化介面之前執行：

```text
python -m unittest -v test_acceptance

test_01_unapproved_and_rejected_never_move ... ok
test_02_approval_binds_exact_content_and_revision ... ok
test_03_execute_checks_inventory_and_persists_result ... ok
test_04_parallel_replays_across_processes_and_restart ... ok

Ran 4 tests in 2.855s
OK
```

- 測試經過真實本機 HTTP，沒有 mock 核准、SQLite 或搬運。
- 第四項使用兩個程序、一個 SQLite 檔及 20 個並行 HTTP 請求；確認只有一次搬運、同一收據及一筆 executed 紀錄。
- 後端程序重啟後，原任務再次送出，結果為 ALREADY_PROCESSED、moved=false、搬運總數仍為 1。
- Python 編譯檢查與 `node --check static/app.js` 通過。

## 實際瀏覽器驗證

| 情境 | 觀察結果 |
| --- | --- |
| 初始畫面 | 黃燈、等待核准、A=1、B=0、執行按鈕停用；明確標示示例與非即時 AI。 |
| 拒絕 | 紅燈、已拒絕、貨品仍在 A；A=1、B=0、此任務搬運 0 次；紀錄為 A → A。 |
| 核准 | 綠燈與執行按鈕啟用；A=1、B=0，核准本身未搬運。 |
| 已核准提案修改方向 | 同一 ID 從版本 1 變成 2，黃燈、等待核准，執行按鈕重新停用。 |
| 核准後執行 | A=0、B=1，此任務搬運 1 次，完成後拒絕／核准／修改／執行均停用。 |
| 頁面重新載入 | SQLite 中的版本與紀錄仍可讀取，不會因重新載入重新初始化。 |
| 桌面 1440×1000 | 左側倉儲、中間提案與燈號、右側紀錄三欄成立。 |
| 小螢幕 390×844 | 內容堆疊、按鈕與文字可讀，頁面沒有水平溢出。 |
| 主動停止本機後端 | 顯示「連線中斷 · 顯示最後確認狀態」，仍顯示 A=0、B=1，建立與操作停用。 |
| 連線正常的操作過程 | 瀏覽器 console 無 warning 或 error。停止伺服器的測試會產生預期的連線錯誤。 |

截圖：[拒絕後維持原位](docs/rejected.png)、[後端確認搬運完成](docs/completed.png)。

以上驗證只涵蓋本機模擬，不包含真實硬體、登入、外部服務或雲端。測試資料與交付時的新展示資料分開保存。
