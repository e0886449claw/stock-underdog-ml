# PR 模板

## 🛰️ 功能描述

將 `stock-underdog-ml` 大衛選股系統與 HERMES 主資料庫打通，建立交叉驗證資料橋樑。

## 🔗 相關問題

無（新功能）

## 📋 變更內容

### 1. SQLite 旁路整合 (`data/sqlite_resonance_sink.py`)

純新增旁路輸出，不改動任何策略運算、DuckDB、Supabase 既有程式碼：
- 寫入 `hermes_data.db` 的 `david_stock_signals` 表
- `run_mode` 分流（dry_run vs production），UNIQUE key 含 run_mode 防測試混入決策
- `is_triple_resonance` 雙保險偵測（boolean → emoji fallback）
- `hit_combinations` 儲存具體雙重符合組合（玄鐵+LSTM / 玄鐵+法人 / LSTM+法人）供任務6兩層分級篩選

### 2. 管線接線 (`main.py`, `pipeline/orchestrator.py`)

- `main.py`: 由 `--dry-run` flag 推導 `run_mode`
- `orchestrator.run_index/run_all_indices`: 新增 `run_mode` 參數，永遠寫入 SQLite（dry-run 也寫，供管道驗證）

### 3. AI 敘述引擎修復 (`evaluators/ai_narrative.py`)

- 放寬內容長度閾值：`>20` → `>0`（strip 後非空即接受）
- 偶發空回傳時重試一次（Agnes flash 端偶發性問題，0.04% 機率）

### 4. 排程與報告 (`reports/david_tw_*`, `reports/david_us_*`)

- 台股盤前 08:00 cron（`df108754bbb6`）
- 美股盤前 20:30 cron（`08be328e96c5`）
- 生產運行報告 11 筆（9/6~9/11）

### 5. 清理

- 刪除過時 cache/stock_lists.json（1348行 → 移除）

## ✅ 驗證

- dry-run 階段：166 筆（0 triple / 65 double）
- production 階段：275 筆（0 triple / 64 double）
- 交叉比對命中：2603長榮 / 4958臻鼎-KY / 2368金像電 / 1229聯華 / 2385群光

## 📦 依賴

無額外依賴，沿用現有 `sqlite3` stdlib

## 🧪 測試建議

1. 執行 `python main.py --dry-run` 確認 SQLite 旁路寫入
2. 檢查 `david_stock_signals` 表 `run_mode='dry_run'` 記錄
3. 執行 `python main.py --market tw` production 模式確認 production 記錄分離
