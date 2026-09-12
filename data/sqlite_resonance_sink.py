"""
SQLite Resonance Sink for David Stock Underdog ML.

將大衛選股系統 (stock-underdog-ml) 跑完的 `report_dict` 結果旁路寫入
HERMES 主資料庫 hermes_data.db 的新表 `david_stock_signals`,作為
「我方策略 vs 大衛選股」交叉驗證的資料橋樑。

設計重點 (指揮官核准的雙保險 + 分層信心 + run_mode 分流)：
1. is_triple_resonance 採「優先讀 boolean → fallback 字串比對 emoji → 抓不到跳 warning」雙保險
2. 額外存 hit_combinations (雙重符合的具體組合: 玄鐵+LSTM / 玄鐵+法人 / LSTM+法人)
   供任務6做「高信心 / 中信心」兩層分級篩選
3. run_mode 分流 (dry_run / production) — 指揮官裁示二核准:
   - main.py --dry-run → run_mode='dry_run'
   - 正式排程 → run_mode='production'
   - UNIQUE key 含 run_mode → 同日 dry_run 與 production 併存不互蓋
   - 任務6交叉比對 SQL 須加 WHERE run_mode='production' 防測試資料混入決策
4. 不改動任何策略運算、DuckDB、Supabase 既有程式碼 — 純新增旁路
"""
import os
import datetime
import sqlite3
import logging
from typing import Dict, Any, List, Optional

from core.config import config  # 沿用專案 config (但路徑採環境變數優先)

logger = logging.getLogger("stock_app.sqlite_sink")

# 主資料庫路徑: 環境變數優先, fallback 到 HERMES 既定路徑
HERMES_DB_PATH = os.getenv(
    "HERMES_DB_PATH",
    "/Users/eddyhermes/Agnes/databases/hermes_data.db"
)

# 三重共振 tag 字串 (composite_evaluator.py L193 寫入) — 供 fallback 比對
TRIPLE_RESONANCE_TAG = "🏆三重共振"


class SQLiteResonanceSink:
    """把 report_dict 的候選標的寫入 hermes_data.db 的 david_stock_signals"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or HERMES_DB_PATH
        # 與 DuckDBManager 對齊的 enabled 語意: 只要路徑存在即可啟用
        self.enabled = bool(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        """建立 SQLite 連線 (WAL 模式降低 lock 衝突)"""
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL;")
        return con

    def ensure_table(self, con: sqlite3.Connection) -> None:
        """建立 david_stock_signals 表 (若不存在)"""
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS david_stock_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                index_name      TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                fetch_date      TEXT NOT NULL,
                run_mode        TEXT DEFAULT 'production',
                signal_type     TEXT,
                potential_pct   REAL,
                target_price    REAL,
                current_price   REAL,
                composite_score REAL,
                is_triple_resonance  INTEGER DEFAULT 0,
                hit_combinations     TEXT,
                hit_count       INTEGER DEFAULT 0,
                tags            TEXT,
                trust_net_5d    REAL,
                foreign_net_5d  REAL,
                raw_json        TEXT,
                created_at      TEXT NOT NULL,
                UNIQUE(index_name, ticker, fetch_date, run_mode, signal_type)
            )
            """
        )
        con.commit()

    @staticmethod
    def _detect_triple_resonance(entry: Dict[str, Any]) -> Optional[bool]:
        """
        雙保險偵測三重共振。

        優先 1: entry 內若有 boolean 欄位 is_triple_resonance 直接用。
        優先 2: tags 內含 emoji 字串 "🏆三重共振" → True。
        找不到 → None (呼叫端負責 warning)。
        """
        # 優先: 真 boolean 欄位
        direct = entry.get("is_triple_resonance")
        if isinstance(direct, bool):
            return direct

        # fallback: tags 字串比對 (含 emoji)
        tags = entry.get("tags", []) or []
        if isinstance(tags, list) and tags:
            if any(TRIPLE_RESONANCE_TAG in str(t) for t in tags):
                return True

        return None  # 無法判斷

    @staticmethod
    def _hit_combinations(hit_strategies: Any, tags: Any) -> str:
        """
        從 hit_strategies / tags 推出「雙重符合的具體組合」。

        我方需要的三種中信心組合 (指揮官定義):
           玄鐵+LSTM / 玄鐵+法人 / LSTM+法人
        回傳以 | 分隔的字串, 如 "玄鐵+LSTM|LSTM+法人"
        """
        combos = []

        has_xuantie = False
        has_lstm = False
        has_inst = False

        # 從 hit_strategies (list of strategy_name) 判斷
        hs = hit_strategies if isinstance(hit_strategies, list) else []
        for s in hs:
            s_lower = str(s).lower()
            if "xuantie" in s_lower or "玄鐵" in s:
                has_xuantie = True
            elif "lstm" in s_lower:
                has_lstm = True
            elif "institutional" in s_lower or "法人" in s:
                has_inst = True

        # 從 tags 字串再補判斷 (玄鐵買點 / LSTM看漲 / 土洋合買|投信)
        tag_list = tags if isinstance(tags, list) else []
        tag_str = " | ".join(str(t) for t in tag_list)
        if "玄鐵買點" in tag_str:
            has_xuantie = True
        if "LSTM" in tag_str:
            has_lstm = True
        if ("土洋合買" in tag_str or "投信" in tag_str):
            has_inst = True

        # 盤點任意兩者成立的組合
        if has_xuantie and has_lstm:
            combos.append("玄鐵+LSTM")
        if has_xuantie and has_inst:
            combos.append("玄鐵+法人")
        if has_lstm and has_inst:
            combos.append("LSTM+法人")

        return "|".join(combos) if combos else ""

    def save_resonance_results(
        self,
        index_name: str,
        results: Dict[str, Any],
        period: str = "6mo",
        macro_state: Any = None,
        run_mode: str = "production",
    ) -> int:
        """把 report_dict 寫入 david_stock_signals, 回傳寫入筆數

        run_mode: 'dry_run' | 'production'。指揮官裁示二核准分流,
        寫入 run_mode 欄位供任務6加 WHERE run_mode='production' 防測試資料混入決策。
        """
        if not self.enabled:
            logger.info("⏭️ SQLite sink disabled (db_path 為空)")
            return 0

        fetch_date = datetime.datetime.now().strftime("%Y-%m-%d")
        timestamp = datetime.datetime.now().isoformat()

        # 候選標的來源: report.ranked_stocks (最完整, 含 tags/institutional/hit_strategies)
        # results 是 report_dict, 但 ranked_stocks 需從原本的 EvaluationReport 取。
        # 此處入參 results 即 orchestrator 的 report_dict,
        # 但 ranked_stocks 不在 report_dict 內 → 需由 orchestrator 額外傳入 rankeds。
        rankeds = results.get("_ranked_stocks_sink", [])
        if not isinstance(rankeds, list) or not rankeds:
            logger.warning("⚠️ sqlite sink: 未取得 ranked_stocks 資料, 本模組由 orchestrator 提供 _ranked_stocks_sink")
            return 0

        macro_regime = getattr(macro_state, "regime_name", None) if macro_state else None
        inst_summaries = results.get("institutional_summaries", {})

        rows_to_write = []
        for entry in rankeds:
            ticker = entry.get("ticker")
            if not ticker:
                continue

            # 雙保險偵測三重共振
            triple = self._detect_triple_resonance(entry)
            if triple is None:
                all_tags = entry.get("tags", [])
                if all_tags and not isinstance(all_tags, list):
                    all_tags = [all_tags]
                if isinstance(all_tags, list) and all_tags:
                    logger.warning(
                        f"⚠️ [is_triple_resonance 無法判斷] {ticker} — 找不到 boolean 欄位也找不到"
                        f"「{TRIPLE_RESONANCE_TAG}」: tags={all_tags}. 請檢查 composite_evaluator 是否改了標籤格式."
                    )
                is_triple = 0
            else:
                is_triple = 1 if triple else 0

            # 雙重符合的具體組合: 從 hit_strategies + tags 推斷
            hit_combos = self._hit_combinations(entry.get("hit_strategies"), entry.get("tags"))
            hit_count = int(entry.get("hit_count", 0) or 0)

            # 從 entry 與 inst_summaries 取法人數據
            inst_meta = entry.get("institutional") or inst_summaries.get(ticker, {})
            if not isinstance(inst_meta, dict):
                inst_meta = {}

            # signal_type 分層: 高信心=triple / 中信心=double / 其他=策略個別
            if is_triple:
                signal_type = "triple"
            elif hit_count >= 2 or hit_combos:
                signal_type = "double"
            else:
                signal_type = "single"

            # target_price / potential
            potential = entry.get("lstm_potential")
            if potential is None:
                potential = entry.get("potential")
            target_price = entry.get("predicted_price")
            current_price = entry.get("current_price")

            row = {
                "index_name": index_name,
                "ticker": ticker,
                "fetch_date": fetch_date,
                "run_mode": run_mode,
                "signal_type": signal_type,
                "potential_pct": float(potential) if potential is not None else None,
                "target_price": float(target_price) if target_price is not None else None,
                "current_price": float(current_price) if current_price is not None else None,
                "composite_score": float(entry["composite_score"]) if entry.get("composite_score") is not None else None,
                "is_triple_resonance": is_triple,
                "hit_combinations": hit_combos,
                "hit_count": hit_count,
                "tags": " | ".join(str(t) for t in (entry.get("tags") or [])),
                "trust_net_5d": inst_meta.get("trust_net_5d"),
                "foreign_net_5d": inst_meta.get("foreign_net_5d"),
                "raw_json": __import__("json").dumps(entry, ensure_ascii=False, default=str),
                "created_at": timestamp,
            }
            rows_to_write.append(row)

        if not rows_to_write:
            logger.warning(f"⚠️ sqlite sink: {index_name} 無候選標的可寫入")
            return 0

        con = self._connect()
        try:
            self.ensure_table(con)
            cols = [
                "index_name", "ticker", "fetch_date", "run_mode", "signal_type", "potential_pct",
                "target_price", "current_price", "composite_score", "is_triple_resonance",
                "hit_combinations", "hit_count", "tags", "trust_net_5d", "foreign_net_5d",
                "raw_json", "created_at",
            ]
            placeholders = ", ".join(["?"] * len(cols))
            col_list = ", ".join(cols)
            upsert = f"""
                INSERT INTO david_stock_signals ({col_list})
                VALUES ({placeholders})
                ON CONFLICT(index_name, ticker, fetch_date, run_mode, signal_type)
                DO UPDATE SET
                    potential_pct=excluded.potential_pct,
                    target_price=excluded.target_price,
                    current_price=excluded.current_price,
                    composite_score=excluded.composite_score,
                    is_triple_resonance=excluded.is_triple_resonance,
                    hit_combinations=excluded.hit_combinations,
                    hit_count=excluded.hit_count,
                    tags=excluded.tags,
                    trust_net_5d=excluded.trust_net_5d,
                    foreign_net_5d=excluded.foreign_net_5d,
                    raw_json=excluded.raw_json
            """
            con.executemany(upsert, [tuple(r[c] for c in cols) for r in rows_to_write])
            con.commit()
            # 實際寫入筆數
            cur = con.execute(
                "SELECT ticker, signal_type FROM david_stock_signals WHERE fetch_date=? AND index_name=?",
                (fetch_date, index_name),
            )
            written = cur.fetchall()
            logger.info(f"✅ SQLite sink 寫入 {index_name}: {len(rows_to_write)} 筆處理 / {len(written)} 筆落表 "
                        f"(db={self.db_path}, date={fetch_date})")
            # 統計高/中信心
            n_triple = sum(1 for w in written if self._is_triple_row(w))
            logger.info(f"   ★ 其中三重共振(高信心)={n_triple}, 其餘=中信心雙重/單策略")
            return len(written)
        except sqlite3.Error as e:
            logger.error(f"❌ SQLite sink 寫入失敗: {e}")
            return 0
        finally:
            con.close()

    @staticmethod
    def _is_triple_row(w) -> bool:
        """row 是否為 triple (row 為 SQLite tuple: (ticker, signal_type))"""
        try:
            return w[1] == "triple"
        except (IndexError, TypeError):
            return False


# 模組層級單例 (對齊 DuckDBManager 慣例: orchestrator 直接 import 使用)
sqlite_resonance_sink = SQLiteResonanceSink()