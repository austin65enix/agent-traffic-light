# Agent Traffic Light｜倉儲任務紅綠燈

**DEVDAY-AGENT-TRAFFIC-LIGHT-M1** — 可離線展示的單頁 Web App。

固定 A、B 兩個貨格及 BOX-001 一件貨品。使用者檢視明確標示的示例提案，核准或拒絕，再由本機後端檢查並執行模擬搬運。無即時 AI、API 金鑰、登入、外部服務、雲端或硬體依賴。此目錄是完整獨立專案，沒有引用或修改既有 GCE 專案。

## 一分鐘介紹

[觀看一分鐘影片：Agent Traffic Light](https://youtu.be/OIhhxtYD4QE)（繁體字幕與中文合成旁白）

**拒絕：貨品仍在 A，這筆任務搬運 0 次。**

![拒絕後貨品仍在 A](docs/rejected.png)

**核准並執行：後端確認後，貨品才移到 B，這筆任務只搬運 1 次。**

![後端確認搬運完成](docs/completed.png)

## 啟動

需要 Python **3.10 以上**及一般瀏覽器；僅用 Python 標準函式庫，不用 pip、npm 或網路安裝。

在本專案目錄開啟 PowerShell：

```powershell
python app.py
```

若電腦使用 Python Launcher，可改用 `py -3 app.py`。Windows 也可雙擊 `start.cmd`。

開啟 **http://127.0.0.1:8765**。終端機按 `Ctrl+C` 停止。重新啟動會保留原有倉儲、提案與紀錄。

若連接埠已占用：

```powershell
python app.py --port 8766
```

然後開啟 http://127.0.0.1:8766。不要直接開啟 `static/index.html`，它需要本機後端。

## 操作

1. 首次啟動：貨品在 A，B 為空，系統建立一筆 A → B 的示例提案。
2. **拒絕此提案**：顯示紅燈、這筆任務搬運 0 次、貨品原位與 `A → A` 紀錄；該任務不能重新啟用。
3. **建立示例提案**：依貨品目前位置建立新 UUID 的搬運提案，仍須核准。
4. **核准此版本**：顯示綠燈，但尚未搬運；核准涵蓋此 ID、版本、來源、目的地、貨品、數量及示例來源。
5. **執行已核准搬運**：後端確認並提交交易後，畫面才更新位置。
6. **修改提案**：可修改 A、B 間的方向，貨品及數量固定為 BOX-001 × 1。儲存即增加版本、清除核准；即使改回原內容也須重新核准。
7. 已拒絕與已處理任務不可修改。可用「檢視提案」選單查看舊任務；左側始終顯示倉儲目前位置，該任務搬運次數另行標示。

桌面為左側倉儲、中間提案與紅綠燈、右側紀錄。較窄視窗會換行，手機會堆疊。失去連線時保留最後確認畫面、顯示連線中斷並停止新操作。

## 四項驗收測試

```powershell
python -m unittest -v test_acceptance
```

測試使用暫存資料庫及隨機本機連接埠，不會更動展示資料。第四項會啟動額外的本機 Python 程序，測試結束自動清理。

| 測試 | 操作與驗收條件 |
| --- | --- |
| 01 未核准與拒絕 | 直接送 HTTP 執行請求也被後端攔截；拒絕後不能重新核准、修改或執行；A=1、B=0、搬運=0；紀錄前後位置相同。 |
| 02 精確版本核准 | 核准後修改方向，原核准失效；舊版本核准／執行／拒絕均被擋；改回原內容仍須重核；不接受額外執行內容及無效數量。 |
| 03 搬運與持久化 | 核准本身不搬運；執行才讓 A=0、B=1；其他提案來源已空時被擋；重新開啟 SQLite 後倉儲與紀錄一致。 |
| 04 重送、並行與重啟 | 同一任務由兩個程序承接 20 個並行 HTTP 請求，只出現 1 筆搬運及同一收據；重啟程序再送仍為 1 次。 |

驗證環境與結果見 [驗證紀錄](ACCEPTANCE.md)。

## 本機儲存與執行保證

- 預設資料檔：本專案的 `data/warehouse.sqlite3`，不受啟動所在工作目錄影響。SQLite WAL 使用時可能有同名 `-wal`、`-shm` 檔。
- `tasks` 保存 UUID、版本、內容雜湊、核准雜湊與狀態；`warehouse` 保存唯一貨品的位置；`movements` 保存每筆搬運的收據；`events` 保存操作與攔截紀錄，含當時提案內容及前後位置。
- 核准內容使用穩定 JSON 的 SHA-256，包含 UUID、revision、origin、source、destination、item、quantity。雜湊用於內容綁定，不是身分簽章。
- **`BEGIN IMMEDIATE`** 在同一 SQLite 檔上串行化寫入，包括不同執行緒與不同程序。核准檢查、來源庫存檢查、庫存變更、任務完成、唯一搬運收據及紀錄在同一交易完成。
- `movements.task_id` 有 **UNIQUE** 約束。一筆任務最多一筆搬運；已處理任務重送回傳原收據及 `moved: false`。
- 規則攔截會回復該操作的修改後寫入攔截紀錄。SQLite 寫入失敗不會讓前端預先移動貨品。
- GET 使用同一讀取交易產生一致狀態。POST 在提交成功後才回傳確認狀態。前端每兩秒讀取、操作時等待結果，並以遞增狀態序號忽略較舊回應。
- 畫面只顯示最新 100 筆事件，資料庫保留完整紀錄。全量提案與搬運仍保留於本機。

這項「最多搬運一次」保證適用於 SQLite 內的模擬搬運。此版沒有真實硬體或外部副作用；未涵蓋硬體控制、登入權限或分散式資料庫。只監聽 `127.0.0.1`，拒絕非本機 Host、跨網站 Origin 與非 JSON 操作；本機使用者仍被視為可信。

## API 摘要

| 方法 / 路徑 | 用途 |
| --- | --- |
| `GET /api/state` | 取得倉儲、提案、收據與最近紀錄的一致快照 |
| `POST /api/proposals` | JSON `{}` 建立一筆新的示例提案 |
| `POST /api/proposals/{id}/approve` | 核准目前確切版本 |
| `POST /api/proposals/{id}/reject` | 終止此任務，不搬運 |
| `POST /api/proposals/{id}/edit` | 修改方向、增加版本、清除原核准 |
| `POST /api/proposals/{id}/execute` | 後端檢查並搬運，或回傳原收據 |

每筆操作都需 `Content-Type: application/json`。approve、reject、execute 的 JSON 只能含 `expected_revision` 與 `expected_fingerprint`（來自 GET）。edit 另需 `source`、`destination`、`item`、`quantity`。前端不能以執行請求替換已核准內容。狀態衝突回傳 HTTP 409，格式錯誤為 400，無效提案為 422。

## 展示與獨立資料集

一分鐘操作稿見 [DEMO.md](DEMO.md)。若要從 A=1、B=0 開始一場全新展示，指定**尚不存在的新資料庫檔名**，保留原紀錄：

```powershell
python app.py --db data/demo-session-02.sqlite3 --port 8766
```

每個新資料庫初始化一次。重用同一路徑會載入既有狀態，不會自動重置。

## 專案內容

```text
app.py                  HTTP 後端、規則、SQLite 交易與本機啟動入口
static/index.html       三欄單頁介面
static/app.js           後端確認後才更新的前端互動
static/style.css        桌面與小螢幕版面
static/favicon.svg      紅綠燈圖示
test_acceptance.py      四項 HTTP 驗收（含跨程序並行與重啟）
start.cmd               Windows 啟動入口
README.md               啟動、操作、資料與 API 說明
DEMO.md                 一分鐘 Demo 操作稿
ACCEPTANCE.md           已執行驗證紀錄
docs/rejected.png       拒絕後貨品仍在 A 的實際瀏覽器截圖
docs/completed.png      後端確認搬運完成的實際瀏覽器截圖
data/                   首次執行後產生；不放入原始碼壓縮檔
```
