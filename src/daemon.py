from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import shutil
import signal
import time

from . import alerts
from .config import load_config
from .executor import Executor
from .models import Order, OrderType, Side
from .nonce import NonceManager
from .parse import order_from_dict, order_to_dict
from .rpc import RpcPool
from .store import Store
from .triggers import TriggerEngine

log = logging.getLogger("daemon")


class Daemon:
    """Резидентный процесс: приём ордеров (inbox-папка) + тик движка триггеров.

    Один процесс держит и стопы по 4 телам, и приём новых ордеров — то, что в
    скелете было невозможно (блокирующий loop vs одноразовый runner).

    Протокол inbox: класть .json файлы (один ордер, список ордеров, либо
    управляющий объект {"cancel": "<client_order_id>"}). Файл после обработки
    уезжает в inbox/processed. Результаты — в results/ и в журнал филлов (SQLite).
    """

    def __init__(self, cfg, inbox: str, results: str, prefer_direct: bool = True):
        self.cfg = cfg
        self.pool = RpcPool(cfg.rpc_urls)
        self.nonce = NonceManager(self.pool)
        self.store = Store(cfg.state_db)
        self.ex = Executor(cfg, self.pool, self.nonce, prefer_direct=prefer_direct)
        self.engine = TriggerEngine(cfg, self.ex, self.store)
        self.inbox = inbox
        self.processed = os.path.join(inbox, "processed")
        self.failed = os.path.join(inbox, "failed")   # B3: битые ордера сюда, НЕ в processed
        self.results = results
        os.makedirs(self.inbox, exist_ok=True)
        os.makedirs(self.processed, exist_ok=True)
        os.makedirs(self.failed, exist_ok=True)
        os.makedirs(self.results, exist_ok=True)
        self._breaker_tripped: set[str] = set()   # имена тел, у кого сработал drawdown
        self._nonce_stuck: dict[str, int] = {}     # C1: сколько тиков подряд адрес «завис»

    # ---------- маршрутизация одного ордера ----------
    def process_order(self, order: Order) -> dict:
        # идемпотентность
        if self.store.seen(order.key()):
            log.info("дубликат ордера %s — пропуск", order.key())
            return {"ok": True, "skipped": "duplicate", "client_order_id": order.client_order_id}

        # circuit-breaker по просадке тела: на BUY новые позиции не открываем. seen НЕ
        # помечаем — чтобы мозг мог переслать ордер после снятия брейкера.
        if order.side == Side.BUY and order.wallet in self._breaker_tripped:
            log.warning("breaker тела %s активен (drawdown) — BUY %s отклонён", order.wallet, order.token)
            return {"ok": False, "error": f"drawdown breaker тела {order.wallet}", "tag": order.tag}

        if order.type == OrderType.LIMIT:
            # регистрация + seen ОДНОЙ транзакцией (атомарно): OOM между ними иначе = двойная лимитка.
            tid = self.engine.register_limit(order, seen_key=order.key())
            log.info("LIMIT принят #%s %s %s @%s", tid, order.side.value, order.token, order.trigger_price)
            return {"ok": True, "registered": "limit", "id": tid, "client_order_id": order.client_order_id}

        # MARKET — исполняем сразу. seen помечаем ПО ФАКТУ broadcast (#1): транзиентный
        # фейл ДО отправки → ордер НЕ помечен → мозг может переслать, он не теряется.
        # on_sent фиксирует «отправлено» (хэш в персисте до ожидания/краша — #2).
        market = order_from_dict({**order_to_dict(order), "type": "market"})
        on_sent = lambda txh: self.store.mark_seen(order.key())
        res = self.ex.execute(market, on_sent=on_sent, confirm_secs=self.cfg.loop_confirm_secs)
        self.engine._journal(market, res)
        # ok ИЛИ pending (tx уже в мемпуле) → повтор не нужен, помечаем seen окончательно.
        if res.ok or res.pending:
            self.store.mark_seen(order.key())
            if order.side == Side.BUY and self.engine.has_conditions(order):
                entry = res.avg_price
                if not entry:
                    # фил неизвестен (tx в мемпуле/таймаут) → entry≈текущая цена, чтобы
                    # TP/SL следили за позицией, а не висели без защиты (#2).
                    try:
                        entry = self.engine._price(order.token)
                    except Exception:
                        entry = None
                if entry:
                    pid = self.engine.register_position(order, entry)
                    if pid > 0:
                        log.info("позиция #%s открыта entry=%.3e, TP/SL/trail активны", pid, entry)
            # #4: маркет не подтвердился в окне (pending) → трек-строка kind='market', чтобы
            # _resolve_exiting добил по receipt и дописал филл (иначе сделка не попадёт в
            # счётчик сделок/день → недосчёт min_trades; + не теряем след tx).
            if res.pending and res.tx_hash:
                # #4: сразу 'exiting'+exit_tx одним коммитом — без зависшей строки при краше.
                self.store.add_trigger(
                    kind="market", wallet=order.wallet, token=order.token, side=order.side.value,
                    order=order_to_dict(market), client_order_id=order.client_order_id,
                    status="exiting", exit_tx=res.tx_hash)
        # жёсткий фейл (не ok, не pending) → seen НЕ помечен → ордер переотправляем позже.
        return _result_dict(market, res)

    def handle_file(self, path: str) -> None:
        base = os.path.basename(path)
        try:
            with open(path) as f:
                payload = json.load(f)
        except Exception as e:
            # B3: битый/полузаписанный JSON НЕ теряем молча в processed — кладём в failed/
            # и пишем .result с ошибкой, чтобы мозг увидел, что ордер не исполнен.
            # (Мозг ДОЛЖЕН писать атомарно: <name>.json.tmp → rename в <name>.json.)
            log.error("битый файл %s: %s — в failed/", path, e)
            with open(os.path.join(self.results, base + ".result"), "w") as rf:
                json.dump([{"ok": False, "error": f"битый JSON: {e}", "file": base}], rf, ensure_ascii=False)
            shutil.move(path, os.path.join(self.failed, base))
            return

        items = payload if isinstance(payload, list) else [payload]
        out = []
        for it in items:
            try:
                if isinstance(it, dict) and "cancel" in it:
                    n = self.store.cancel_trigger(it["cancel"])
                    # clear_seen ТОЛЬКО если реально отменили лимитку (n>0). Иначе: лимитка уже
                    # стала позицией с тем же coid, cancel её не трогает (K3), а снятый seen
                    # пропустил бы повторный market-BUY на живой позиции (MED1).
                    if n > 0:
                        self.store.clear_seen(it["cancel"])
                    out.append({"cancelled": it["cancel"], "count": n})
                    continue
                out.append(self.process_order(order_from_dict(it)))
            except Exception as e:
                log.exception("ордер упал: %s", e)
                out.append({"ok": False, "error": str(e)})

        base = os.path.basename(path)
        with open(os.path.join(self.results, base + ".result"), "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        shutil.move(path, os.path.join(self.processed, base))

    def scan_inbox(self) -> None:
        for name in sorted(os.listdir(self.inbox)):
            p = os.path.join(self.inbox, name)
            if os.path.isfile(p) and name.endswith(".json"):
                log.info("inbox: %s", name)
                self.handle_file(p)

    # ---------- гейты конкурса (ПЕР-ТЕЛО) ----------
    def _equity_bnb(self, wallet) -> float:
        """Equity тела в BNB = нативный BNB + стоимость всех открытых токен-позиций.

        Считать просадку по голому BNB нельзя (C5): после любой покупки BNB≈резерв,
        стоимость ушла в токен → ложная просадка ~99% и вечная заморозка BUY; и
        наоборот, реальная просадка В ТОКЕНЕ была бы невидима."""
        eq = self.ex.direct.balance_bnb(wallet.address, primary=True) / 1e18  # #7: primary
        seen_tokens = set()
        for row in self.store.active_triggers():
            if row["kind"] != "position" or row["wallet"] != wallet.name:
                continue
            tok = row["token"]
            if tok in seen_tokens:
                continue
            seen_tokens.add(tok)
            try:
                bal = self.ex.direct.balance_token(tok, wallet.address, primary=True)  # #7: primary, не реплика
                if bal <= 0:
                    continue
                dec = self.ex.direct.decimals(tok)
                eq += (bal / 10 ** dec) * self.ex.direct.price_in_bnb(tok)
            except Exception as e:
                log.warning("equity %s: токен %s не оценён (%s)", wallet.name, tok, e)
        return eq

    def _check_breaker(self) -> None:
        """Просадка считается отдельно на каждое тело (свой peak equity, свой порог)."""
        for w in self.cfg.wallets:
            thr = self.cfg.params_for(w.name).max_drawdown_pct
            if thr is None:
                self._breaker_tripped.discard(w.name)
                continue
            try:
                eq = self._equity_bnb(w)
            except Exception:
                continue
            key = f"peak_equity_bnb:{w.name}"
            peak = float(self.store.get_meta(key) or 0)
            if eq > peak:
                self.store.set_meta(key, str(eq))
                peak = eq
            if peak <= 0:
                continue
            dd = (peak - eq) / peak
            if dd >= thr:
                if w.name not in self._breaker_tripped:
                    log.error("тело %s DRAWDOWN %.1f%% ≥ %.1f%% — BUY заморожен (дисквал-риск)",
                              w.name, dd * 100, thr * 100)
                self._breaker_tripped.add(w.name)
            else:
                self._breaker_tripped.discard(w.name)

    def _check_min_trades(self) -> None:
        day_start = time.time() - (time.time() % 86400)  # UTC-сутки
        for w in self.cfg.wallets:
            need = self.cfg.params_for(w.name).min_trades_per_day
            if need is None:
                continue
            n = self.store.trades_since(day_start, w.name)
            if n < need:
                log.info("тело %s: сделок сегодня %d / минимум %d (правило конкурса)",
                         w.name, n, need)

    # C1: сторож nonce-дыры. ВАЖНО (N1): выдержка ДОЛЖНА превышать EXITING_TTL свопа (90с),
    # иначе на приватном relay (48.club не показывает СВОЮ pending-tx в getTransactionCount)
    # сторож примет «в полёте» за «дыру» и форс-ресинкнет nonce, пока tx ещё может смайниться.
    # 8 тиков × poll 15с = 120с > 90с. Основной механизм восстановления — resync в
    # _resolve_exiting по ДОКАЗАННОМУ непрохождению; сторож — последний рубеж против зависания.
    GAP_TICKS = 8

    def _check_nonce_gaps(self) -> None:
        from .triggers import EXITING_TTL_SECS
        # N1: выдержка обязана быть больше TTL свопа в тиках — иначе ресинкнем tx в полёте.
        gap_ticks = max(self.GAP_TICKS, int(EXITING_TTL_SECS / max(1, self.cfg.poll_secs)) + 2)
        for w in self.cfg.wallets:
            try:
                stuck = self.nonce.is_gap(w.address)
            except Exception:
                continue  # RPC недоступен — не трогаем
            if not stuck:
                self._nonce_stuck[w.address] = 0
                continue
            n = self._nonce_stuck.get(w.address, 0) + 1
            self._nonce_stuck[w.address] = n
            if n >= gap_ticks:
                log.error("тело %s (%s): nonce-дыра %d тиков — форс-resync (C1)", w.name, w.address, n)
                self.nonce.resync(w.address)
                self._nonce_stuck[w.address] = 0

    # #3: при нехватке RAM (>=95% занято) подчищаем старое (терминальные триггеры/старые
    # филлы/обработанные файлы) — чтобы рост состояния не привёл к OOM (на 167 OOM был).
    MEM_PRUNE_PCT = 95.0

    def _mem_used_pct(self) -> float:
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for ln in f:
                    k, v = ln.split(":")[0], ln.split()[1]
                    mem[k] = int(v)  # kB
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", 0)
            return (1 - avail / total) * 100 if total else 0.0
        except Exception:
            return 0.0

    def _check_memory(self) -> None:
        pct = self._mem_used_pct()
        if pct < self.MEM_PRUNE_PCT:
            return
        pruned = self.store.prune_old()
        # подчистить обработанные/битые файлы (оставляем недавние не нужно — это история)
        removed = 0
        for d in (self.processed, self.failed):
            try:
                for name in os.listdir(d):
                    os.remove(os.path.join(d, name)); removed += 1
            except Exception:
                pass
        log.warning("RAM %.0f%% >= %.0f%% — подчистка: триггеры %d, филлы %d, seen %d, файлы %d",
                    pct, self.MEM_PRUNE_PCT, pruned["triggers"], pruned["fills"], pruned["seen"], removed)

    # ---------- цикл ----------
    def tick(self) -> None:
        # #6: стопы/выходы ПЕРВЫМИ — иначе медленный ордер в инбоксе (до ~confirm_secs)
        # задержал бы проверку триггеров и можно проспать стоп.
        self.engine.run_once()
        self.scan_inbox()
        self._check_nonce_gaps()
        self._check_memory()
        self._check_breaker()
        self._check_min_trades()

    def _acquire_lock(self) -> None:
        """K7: один демон на стейт. Вторая копия (после рестарта/случайно) обошла бы
        CAS-защиту и наделала двойных сделок — на 185 такое уже было. flock рядом с БД."""
        self._lock_path = self.cfg.state_db + ".lock"
        # 'a+' (не 'w'): не затирать pid ДО взятия лока — иначе второй инстанс обнулит файл
        # первого ещё до того, как flock ему откажет.
        self._lock_fh = open(self._lock_path, "a+")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                f"демон уже запущен (lock {self._lock_path} занят) — второй экземпляр не стартую")
        self._lock_fh.seek(0)
        self._lock_fh.truncate()
        self._lock_fh.write(str(os.getpid()))
        self._lock_fh.flush()

    def run(self) -> None:
        self._acquire_lock()
        # K8: по SIGTERM/SIGINT не рвём процесс посреди сделки — доигрываем текущий тик
        # и выходим штатно (restart/OOM иначе мог оставить позицию без присмотра).
        self._stop = False
        def _on_sig(signum, frame):
            log.warning("сигнал %s — гашусь после текущего тика", signum)
            self._stop = True
        signal.signal(signal.SIGTERM, _on_sig)
        signal.signal(signal.SIGINT, _on_sig)

        log.info("daemon старт: inbox=%s rpc=%s tokens_whitelist=%d",
                 self.inbox, self.cfg.rpc_urls[0], len(self.cfg.allowed_tokens))
        alerts.notify(self.cfg.tg_bot_token, self.cfg.tg_chat_id,
                      f"[СТАРТ] twak daemon: {len(self.cfg.wallets)} тел, "
                      f"вайтлист {len(self.cfg.allowed_tokens)} токенов")
        self.engine.reconcile()
        # heartbeat: если эти пинги перестают приходить — демон умер (dead-man's-switch).
        hb_every = max(1, 86400 // max(1, self.cfg.poll_secs))   # ~раз в сутки
        ticks = 0
        errors_since_hb = 0   # H7: heartbeat НЕ должен врать «жив», если каждый тик падает
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                errors_since_hb += 1
                log.exception("tick упал: %s", e)
            ticks += 1
            if ticks % hb_every == 0:
                n = len(self.store.active_triggers())
                if errors_since_hb:
                    # тик падал — это НЕ зелёный heartbeat, а тревога (через log.error → TG).
                    log.error("[HEARTBEAT-ОШИБКА] twak: %d падений тика за период, активных %d — "
                              "торговля НЕ идёт штатно", errors_since_hb, n)
                else:
                    alerts.notify(self.cfg.tg_bot_token, self.cfg.tg_chat_id,
                                  f"[HEARTBEAT] twak жив: активных триггеров {n}, тик без ошибок")
                errors_since_hb = 0
            if self._stop:
                break
            time.sleep(self.cfg.poll_secs)
        log.info("daemon остановлен штатно")
        alerts.notify(self.cfg.tg_bot_token, self.cfg.tg_chat_id, "[СТОП] twak daemon остановлен")


def _result_dict(order: Order, res) -> dict:
    return {
        "ok": res.ok, "status": res.status.value if res.status else None,
        "tx_hash": res.tx_hash, "bscscan": res.bscscan(),
        "filled_amount": res.filled_amount, "avg_price": res.avg_price,
        "gas_used": res.gas_used, "error": res.error,
        "wallet": order.wallet, "token": order.token, "side": order.side.value,
        "tag": order.tag, "client_order_id": order.client_order_id,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="twak_executor — резидентный демон (ордера + триггеры)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--inbox", default="orders_inbox")
    ap.add_argument("--results", default="results")
    ap.add_argument("--use-twak", action="store_true", help="включить путь twak (по умолч. только direct)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    # TG-алерты: все WARNING/ERROR (тихие отказы, nonce-дыры, мёртвые стопы) уходят в чат.
    if alerts.attach_telegram(cfg.tg_bot_token, cfg.tg_chat_id):
        log.info("TG-алерты включены")
    Daemon(cfg, args.inbox, args.results, prefer_direct=not args.use_twak).run()


if __name__ == "__main__":
    main()
