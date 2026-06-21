from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS triggers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,           -- 'limit' (вход) | 'position' (открытая позиция с TP/SL)
    status        TEXT NOT NULL,           -- pending | exiting | closed | cancelled | failed
    wallet        TEXT NOT NULL,
    token         TEXT NOT NULL,
    side          TEXT,                    -- для limit: buy/sell
    order_json    TEXT NOT NULL,           -- исходный Order (для повторного исполнения)
    entry_price   REAL,                    -- BNB за токен на входе (для position)
    hwm           REAL,                    -- high-water mark для trailing
    exit_tx       TEXT,
    client_order_id TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    wallet        TEXT, token TEXT, side TEXT, venue TEXT,
    tx_hash       TEXT, status TEXT,
    filled_amount REAL, avg_price REAL,
    tag TEXT, client_order_id TEXT
);
CREATE TABLE IF NOT EXISTS seen (
    client_order_id TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    """Персист состояния: триггеры (лимит/позиции), журнал филлов, идемпотентность, мета.

    SQLite WAL — переживает рестарт/краш (на 185 уже валил OOM, см. память). Без этого
    рестарт сносит все висящие лимитки и стопы на реальных деньгах.
    """

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        # миграция для уже существующих БД (state.db/state_test.db без колонки attempts)
        try:
            self._db.execute("ALTER TABLE triggers ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
        self._db.commit()

    # ---------- триггеры ----------
    def add_trigger(self, kind: str, wallet: str, token: str, side: Optional[str],
                    order: dict, entry_price: Optional[float] = None,
                    hwm: Optional[float] = None, client_order_id: str = "",
                    seen_key: Optional[str] = None, status: str = "pending",
                    exit_tx: Optional[str] = None) -> int:
        # seen_key: если задан — пометить seen В ТОЙ ЖЕ транзакции, что и вставка триггера.
        # Иначе register_limit + mark_seen — два коммита, и OOM/краш между ними даёт ДВЕ
        # лимитки = двойная покупка (на 167 OOM реален). Один commit = атомарно.
        # status/exit_tx: #4 — pending-market трек-строку пишем сразу 'exiting'+хэш одним
        # коммитом (иначе краш между add_trigger и set_status = зависшая строка).
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO triggers(kind,status,wallet,token,side,order_json,entry_price,"
                "hwm,exit_tx,client_order_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, status, wallet, token, side, json.dumps(order),
                 entry_price, hwm, exit_tx, client_order_id, now, now),
            )
            if seen_key:
                self._db.execute("INSERT OR IGNORE INTO seen(client_order_id,ts) VALUES(?,?)",
                                 (seen_key, now))
            self._db.commit()
            return cur.lastrowid

    def active_triggers(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(
                "SELECT * FROM triggers WHERE status IN ('pending','exiting') ORDER BY id"))

    def set_trigger_status(self, tid: int, status: str, exit_tx: Optional[str] = None) -> None:
        # H2: при закрытии/отмене обнуляем счётчик неудач — иначе на переоткрытой/повторной
        # строке он бы стартовал с прошлого значения и алерт срабатывал бы преждевременно.
        reset_attempts = status in ("closed", "cancelled")
        with self._lock:
            if reset_attempts:
                self._db.execute(
                    "UPDATE triggers SET status=?, exit_tx=COALESCE(?,exit_tx), attempts=0, "
                    "updated_at=? WHERE id=?", (status, exit_tx, time.time(), tid))
            else:
                self._db.execute(
                    "UPDATE triggers SET status=?, exit_tx=COALESCE(?,exit_tx), updated_at=? WHERE id=?",
                    (status, exit_tx, time.time(), tid))
            self._db.commit()

    def claim_trigger(self, tid: int, from_status: str, to_status: str) -> bool:
        """Атомарный CAS: перевести from_status→to_status, только если статус ещё
        from_status. Возвращает True, если ИМЕННО этот вызов выиграл переход.

        Защита от двойного исполнения: только победитель CAS шлёт своп. Второй
        процесс/повторный тик/гонка получат False и не тронут деньги.
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE triggers SET status=?, updated_at=? WHERE id=? AND status=?",
                (to_status, time.time(), tid, from_status))
            self._db.commit()
            return cur.rowcount == 1

    def bump_attempts(self, tid: int) -> int:
        """+1 к счётчику неудачных свопов строки; вернуть новое значение (#5 анти-газ-петля)."""
        with self._lock:
            self._db.execute("UPDATE triggers SET attempts=attempts+1, updated_at=? WHERE id=?",
                             (time.time(), tid))
            self._db.commit()
            row = self._db.execute("SELECT attempts FROM triggers WHERE id=?", (tid,)).fetchone()
            return row["attempts"] if row else 0

    def set_hwm(self, tid: int, hwm: float) -> None:
        with self._lock:
            self._db.execute("UPDATE triggers SET hwm=?, updated_at=? WHERE id=?",
                             (hwm, time.time(), tid))
            self._db.commit()

    def cancel_trigger(self, client_order_id: str) -> int:
        # K3: отменяем ТОЛЬКО лимит-заявки (kind='limit'). Открытая позиция хранится с
        # тем же client_order_id и тоже бывает 'pending' — без фильтра cancel молча гасил
        # бы стоп живой позиции, оставляя токен без защиты.
        with self._lock:
            cur = self._db.execute(
                "UPDATE triggers SET status='cancelled', updated_at=? "
                "WHERE client_order_id=? AND status='pending' AND kind='limit'",
                (time.time(), client_order_id))
            self._db.commit()
            return cur.rowcount

    # ---------- журнал филлов ----------
    def record_fill(self, res_dict: dict) -> None:
        with self._lock:
            # N3: дедуп по tx_hash — резолв может сработать дважды (receipt нашёлся до
            # смены статуса), не хотим двойной строки в журнале сделок.
            txh = res_dict.get("tx_hash")
            if txh:
                dup = self._db.execute(
                    "SELECT 1 FROM fills WHERE tx_hash=? AND status=?",
                    (txh, res_dict.get("status"))).fetchone()
                if dup is not None:
                    return
            self._db.execute(
                "INSERT INTO fills(ts,wallet,token,side,venue,tx_hash,status,filled_amount,"
                "avg_price,tag,client_order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (res_dict.get("ts", time.time()), res_dict.get("wallet"), res_dict.get("token"),
                 res_dict.get("side"), res_dict.get("venue"), res_dict.get("tx_hash"),
                 res_dict.get("status"), res_dict.get("filled_amount"), res_dict.get("avg_price"),
                 res_dict.get("tag"), res_dict.get("client_order_id")))
            self._db.commit()

    def recent_fills(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(
                "SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,)))

    def trades_since(self, ts: float, wallet: Optional[str] = None) -> int:
        with self._lock:
            if wallet:
                row = self._db.execute(
                    "SELECT COUNT(*) c FROM fills WHERE ts>=? AND status='filled' AND wallet=?",
                    (ts, wallet)).fetchone()
            else:
                row = self._db.execute(
                    "SELECT COUNT(*) c FROM fills WHERE ts>=? AND status='filled'", (ts,)).fetchone()
            return row["c"]

    # ---------- идемпотентность ----------
    def seen(self, client_order_id: str) -> bool:
        if not client_order_id:
            return False
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM seen WHERE client_order_id=?", (client_order_id,)).fetchone() is not None

    def mark_seen(self, client_order_id: str) -> None:
        if not client_order_id:
            return
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO seen(client_order_id,ts) VALUES(?,?)",
                             (client_order_id, time.time()))
            self._db.commit()

    def clear_seen(self, client_order_id: str) -> None:
        """Забыть пометку «видел» — при cancel, чтобы повторная подача того же coid прошла."""
        if not client_order_id:
            return
        with self._lock:
            self._db.execute("DELETE FROM seen WHERE client_order_id=?", (client_order_id,))
            self._db.commit()

    def prune_old(self, fills_keep_secs: float = 7 * 86400) -> dict:
        """Подчистить старое (под давлением памяти): терминальные триггеры
        (closed/cancelled/failed) и старые филлы/seen. Активные (pending/exiting) НЕ трогаем."""
        cutoff = time.time() - fills_keep_secs
        with self._lock:
            t = self._db.execute(
                "DELETE FROM triggers WHERE status IN ('closed','cancelled','failed')").rowcount
            f = self._db.execute("DELETE FROM fills WHERE ts < ?", (cutoff,)).rowcount
            s = self._db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,)).rowcount
            self._db.commit()   # сначала закрыть транзакцию...
            try:
                self._db.execute("VACUUM")   # ...VACUUM нельзя внутри транзакции; под OOM может упасть
            except Exception:
                pass            # не критично — место освободится позже
        return {"triggers": t, "fills": f, "seen": s}

    # ---------- мета (peak equity для drawdown и т.п.) ----------
    def get_meta(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._db.commit()
