from __future__ import annotations

import json
import logging
import time

from .config import Config
from .executor import Executor
from .models import Order, OrderType, Side
from .parse import order_from_dict, order_to_dict
from .store import Store

log = logging.getLogger("triggers")

# B1: позицию НЕ переводим в 'failed' по числу неудач — иначе она выпадает из слежения
# и токен остаётся без стопа (хуже, чем газ-петля). Пока просто громко логируем каждые
# ALERT_EVERY_FAILS неудач; политику «N стопов → алерт в чат + действие» добавим со стратегией.
ALERT_EVERY_FAILS = 6

# H1: если exit_tx не находится receipt'ом дольше этого — не ждём вечно, резолвим по
# балансу (на спайке газа tx могла дропнуться; иначе стоп завис бы навсегда).
EXITING_TTL_SECS = 90


class TriggerEngine:
    """Единый движок ценовых триггеров: лимит-вход + take-profit + stop-loss + trailing.

    Ключевая идея: лимитка, TP и SL — один механизм (поллим цену → при пересечении
    порога шлём МАРКЕТ-своп через Executor). Состояние в Store (переживает рестарт).

    Цена — BNB за 1 целый токен, из PancakeSwap через DirectAdapter, независимо от
    того, каким путём открыта позиция.
    """

    def __init__(self, cfg: Config, executor: Executor, store: Store):
        self.cfg = cfg
        self.ex = executor
        self._price_fails: dict[int, int] = {}   # M2: подряд неудачных чтений цены на строку
        self.store = store

    # ---------- регистрация ----------
    def register_limit(self, order: Order, seen_key: str | None = None) -> int:
        """Лимит-ордер: ждать trigger_price, потом маркет-исполнить. seen_key (если задан)
        помечается seen В ОДНОЙ транзакции с регистрацией — без двойной лимитки при OOM."""
        return self.store.add_trigger(
            kind="limit", wallet=order.wallet, token=order.token, side=order.side.value,
            order=order_to_dict(order), client_order_id=order.client_order_id, seen_key=seen_key)

    def effective_conditions(self, order: Order) -> tuple:
        """TP/SL/trailing тела: явное в ордере → дефолт тела (per-body strategy)."""
        p = self.cfg.params_for(order.wallet)
        tp = order.take_profit_pct if order.take_profit_pct is not None else p.take_profit_pct
        sl = order.stop_loss_pct if order.stop_loss_pct is not None else p.stop_loss_pct
        tr = order.trailing_pct if order.trailing_pct is not None else p.trailing_pct
        return tp, sl, tr

    def has_conditions(self, order: Order) -> bool:
        return any(c is not None for c in self.effective_conditions(order))

    def register_position(self, order: Order, entry_price: float) -> int:
        """Открытая лонг-позиция с TP/SL/trailing — следить и закрывать.

        Эффективные пороги тела «запекаются» в сохранённый ордер, чтобы рестарт и
        _eval_position читали уже разрешённые числа.
        """
        # #9: позиция без положительного entry бесполезна — change=(price-entry)/entry
        # выродится и TP/SL/trailing молча не сработают. Лучше явно отказать и залогировать.
        if entry_price is None or entry_price <= 0:
            log.error("позиция %s НЕ открыта: невалидный entry=%s (TP/SL не работали бы)",
                      order.token, entry_price)
            return -1
        tp, sl, tr = self.effective_conditions(order)
        d = order_to_dict(order)
        d["take_profit_pct"], d["stop_loss_pct"], d["trailing_pct"] = tp, sl, tr
        return self.store.add_trigger(
            kind="position", wallet=order.wallet, token=order.token, side=Side.SELL.value,
            order=d, entry_price=entry_price, hwm=entry_price,
            client_order_id=order.client_order_id)

    def _addr_of(self, wallet_name: str):
        return next((w.address for w in self.cfg.wallets if w.name == wallet_name), None)

    # ---------- рекон после рестарта ----------
    def reconcile(self) -> None:
        """После рестарта/крэша: доразрулить висящие 'exiting' (по receipt/балансу) и
        сверить позиции с ончейн-балансом. Без этого зависший 'exiting' либо застрянет,
        либо (в старой версии) ре-стрелял своп каждый тик (двойной расход — C4)."""
        for row in self.store.active_triggers():
            if row["status"] == "exiting":
                try:
                    self._resolve_exiting(row)
                except Exception as e:
                    log.warning("рекон exiting #%s: %s", row["id"], e)
                continue
            if row["kind"] != "position":
                continue
            try:
                addr = self._addr_of(row["wallet"])
                # H1: с primary — отстающая реплика после рестарта вернёт stale balanceOf=0 →
                # позицию ложно закроем → токен без стопа. (resolve уже на primary, тут было нет.)
                bal = self.ex.direct.balance_token(row["token"], addr, primary=True)
            except Exception as e:
                log.warning("рекон %s: баланс недоступен (%s)", row["id"], e)
                continue
            if bal <= 0:
                log.info("рекон: позиция #%s уже без токена → closed", row["id"])
                self.store.set_trigger_status(row["id"], "closed")

    # ---------- тик ----------
    def _price(self, token: str) -> float:
        return self.ex.direct.price_in_bnb(token)

    def _backoff_ok(self, row) -> bool:
        """HIGH#1: после неудачного свопа НЕ бьёмся каждые 15с (жжём BNB на реверзящем токене),
        а ждём растущую паузу. Позицию НЕ бросаем (B1) — просто реже ретраим.
        Пауза = min(30 * 2^(attempts-1), 600с)."""
        n = row["attempts"] or 0
        if n <= 0:
            return True
        backoff = min(30 * (2 ** (n - 1)), 600)
        return (time.time() - (row["updated_at"] or 0)) >= backoff

    def run_once(self) -> None:
        for row in self.store.active_triggers():
            # 'exiting' = своп уже отправлен/в процессе → НЕ переоцениваем и НЕ
            # ре-стреляем своп (это и был C4), а доразруливаем по receipt/балансу.
            if row["status"] == "exiting":
                try:
                    self._resolve_exiting(row)
                except Exception as e:
                    log.exception("резолв exiting #%s упал: %s", row["id"], e)
                continue
            if not self._backoff_ok(row):
                continue   # HIGH#1: ждём бэкофф после прошлых неудач, не жжём газ каждый тик
            try:
                price = self._price(row["token"])
                self._price_fails.pop(row["id"], None)
            except Exception as e:
                # M2: для ОТКРЫТОЙ позиции «нет цены» = пул мог исчезнуть (rug/делист) ровно
                # когда нужен стоп. Молча пропускать опасно — эскалируем в громкий алерт,
                # чтобы тихо не умер стоп (продать всё равно нечем, но человек должен узнать).
                n = self._price_fails.get(row["id"], 0) + 1
                self._price_fails[row["id"]] = n
                if row["kind"] == "position" and n % ALERT_EVERY_FAILS == 0:
                    log.error("ВНИМАНИЕ: позиция #%s токен %s — цена недоступна %d тиков подряд "
                              "(пул исчез/делист?), стоп НЕ оценивается", row["id"], row["token"], n)
                else:
                    log.warning("цена %s недоступна (%d): %s", row["token"], n, e)
                continue
            try:
                if row["kind"] == "limit":
                    self._eval_limit(row, price)
                else:
                    self._eval_position(row, price)
            except Exception as e:
                log.exception("триггер #%s упал: %s", row["id"], e)

    def _resolve_exiting(self, row) -> None:
        """Доразрулить строку, застрявшую в 'exiting' (после pending-tx или крэша).

        Логика: если есть exit_tx — решаем по его receipt; если хэша нет (крэш ДО
        отправки) — по балансу. Ничего не ре-шлём, пока не убедились, что прошлый
        своп не прошёл. Это закрывает двойную покупку/продажу (C4)."""
        tid = row["id"]
        txh = row["exit_tx"]
        if txh:
            # receipt ищем по ЛЮБОЙ ноде (K2): primary=48.club private-MEV его может не
            # отдавать → иначе позиция вечно exiting, стоп не дорабатывает.
            r = self.ex.direct.pool.get_receipt_any(txh)
            if r is not None:
                if r.get("status") == 1:
                    log.info("резолв #%s: tx %s подтверждена → closed", tid, txh)
                    self.store.set_trigger_status(tid, "closed", exit_tx=txh)
                    # дописать филл (K5): иначе resolve-сделка не попадёт в счётчик сделок/день.
                    self.store.record_fill({"wallet": row["wallet"], "token": row["token"],
                                            "side": row["side"], "venue": "direct", "tx_hash": txh,
                                            "status": "filled", "client_order_id": row["client_order_id"]})
                    self._maybe_open_position_after_limit(row)
                    return
                # tx реверзнулась on-chain (status 0) → nonce УЖЕ потрачен (mined), НЕ resync.
                if row["kind"] == "market":
                    # маркет fire-once: не ретраим (мозг сам решит), просто закрываем.
                    log.warning("резолв #%s: market-tx %s реверзнулась → closed (без ретрая)", tid, txh)
                    self.store.set_trigger_status(tid, "closed")
                    return
                log.warning("резолв #%s: tx %s реверзнулась → возврат в pending", tid, txh)
                self.store.set_trigger_status(tid, "pending")
                return
            # receipt не найден. H1: ждём дольше TTL → tx, вероятно, дропнулась (спайк газа),
            # не висим вечно — резолвим по балансу ниже. Иначе ещё ждём.
            age = time.time() - (row["updated_at"] or 0)
            if age <= EXITING_TTL_SECS:
                log.info("резолв #%s: tx %s ещё не подтверждена (%ds) — ждём", tid, txh, int(age))
                return
            log.warning("резолв #%s: tx %s не найдена за %ds (TTL) → решаю по балансу", tid, txh, int(age))

        # Сюда: либо хэша нет (крэш до send), либо TTL по txh истёк. Решаем по балансу —
        # самый надёжный сигнал «прошёл ли своп на самом деле».
        addr = self._addr_of(row["wallet"])
        if row["kind"] == "position":
            try:
                bal = self.ex.direct.balance_token(row["token"], addr, primary=True) if addr else None
            except Exception as e:
                log.warning("резолв #%s: баланс недоступен (%s) — ждём", tid, e)
                return
            if bal is not None and bal <= 0:
                log.info("резолв #%s: токена нет → closed (продажа прошла)", tid)
                self.store.set_trigger_status(tid, "closed")
                # MED2: записать филл в БД и в балансовой ветке (на 48.club receipt часто
                # доступен только так) — иначе исполненная сделка не попадёт в журнал.
                self.store.record_fill({"wallet": row["wallet"], "token": row["token"],
                                        "side": row["side"], "venue": "direct", "tx_hash": txh,
                                        "status": "filled", "client_order_id": row["client_order_id"]})
            else:
                # выход НЕ прошёл → ретраим. C1: tx не майнилась → слот nonce свободен,
                # сбрасываем счётчик, чтобы тело не зависло nonce-дырой.
                log.info("резолв #%s: токен на месте, выход не прошёл → pending (+resync)", tid)
                if addr:
                    self.ex.direct.nonce.resync(addr)
                self.store.set_trigger_status(tid, "pending")
            return
        if row["kind"] == "market":
            # маркет fire-once: подтвердить не смогли — закрываем, мозг сам решит (не ретраим).
            log.info("резолв #%s: market без подтверждения → closed", tid)
            if addr:
                self.ex.direct.nonce.resync(addr)
            self.store.set_trigger_status(tid, "closed")
            return
        # #5 limit без подтверждения по receipt: НЕ ре-пендим вслепую (на медленной ноде tx
        # могла уйти и смайниться позже → ре-файр = двойная покупка). Помечаем failed + алерт;
        # повторную подачу того же coid всё равно блокирует seen (помечен при регистрации).
        log.error("ВНИМАНИЕ: limit #%s (%s %s) не подтверждён за TTL — failed, проверь вручную",
                  tid, row["side"], row["token"])
        if addr:
            self.ex.direct.nonce.resync(addr)
        self.store.set_trigger_status(tid, "failed")

    def _maybe_open_position_after_limit(self, row) -> None:
        """После подтверждённого лимит-BUY с условиями — открыть позицию для слежения.
        entry точно неизвестен (резолв вне live-пути) → берём текущую цену как
        приближение, чтобы TP/SL/trailing всё же защищали позицию."""
        if row["kind"] != "limit":
            return
        order = order_from_dict(json.loads(row["order_json"]))
        if order.side != Side.BUY or not self.has_conditions(order):
            return
        try:
            entry = self._price(row["token"])
        except Exception:
            entry = row["entry_price"] or 0
        if entry:
            pid = self.register_position(order, entry)
            log.info("резолв #%s: открыта позиция #%s entry≈%.3e (приближение)", row["id"], pid, entry)

    def _eval_limit(self, row, price: float) -> None:
        order = order_from_dict(json.loads(row["order_json"]))
        trig = order.trigger_price
        if trig is None:
            return
        fire = (order.side == Side.BUY and price <= trig) or \
               (order.side == Side.SELL and price >= trig)
        if not fire:
            return
        # атомарно забираем строку pending→exiting: только победитель шлёт своп
        # (защита от гонки/второго процесса/повторного тика — C4).
        if not self.store.claim_trigger(row["id"], "pending", "exiting"):
            return
        log.info("LIMIT #%s %s %s @%.3e (порог %.3e) → маркет", row["id"],
                 order.side.value, order.token, price, trig)
        market = order_from_dict(order_to_dict(order))
        market.type = OrderType.MARKET
        on_sent = lambda txh: self.store.set_trigger_status(row["id"], "exiting", exit_tx=txh)
        res = self.ex.execute(market, on_sent=on_sent, confirm_secs=self.cfg.loop_confirm_secs)
        self._journal(market, res)
        if res.pending:
            # tx ушла, но не подтвердилась — хэш уже записан через on_sent.
            # Оставляем 'exiting', доразрулит _resolve_exiting. НЕ ре-шлём.
            log.warning("LIMIT #%s tx не подтверждена (%s) — резолв позже", row["id"], res.tx_hash)
            self.store.set_trigger_status(row["id"], "exiting", exit_tx=res.tx_hash)
            return
        if not res.ok:
            n = self.store.bump_attempts(row["id"])
            if n % ALERT_EVERY_FAILS == 0:
                log.error("LIMIT #%s: %d неудачных свопов подряд (%s) — ВНИМАНИЕ", row["id"], n, res.error)
            else:
                log.warning("LIMIT #%s не исполнился (%s), попытка %d — назад в pending",
                            row["id"], res.error, n)
            self.store.set_trigger_status(row["id"], "pending")  # ретраим, не бросаем (B1)
            return
        self.store.set_trigger_status(row["id"], "closed", exit_tx=res.tx_hash)
        # если это вход с условиями — открыть позицию для слежения
        if order.side == Side.BUY and self.has_conditions(order):
            entry = res.avg_price or price
            self.register_position(order, entry)

    def _eval_position(self, row, price: float) -> None:
        order = order_from_dict(json.loads(row["order_json"]))
        entry = row["entry_price"] or price
        hwm = max(row["hwm"] or entry, price)
        if hwm > (row["hwm"] or 0):
            self.store.set_hwm(row["id"], hwm)

        change = (price - entry) / entry
        hit_tp = order.take_profit_pct is not None and change >= order.take_profit_pct
        hit_sl = order.stop_loss_pct is not None and change <= -order.stop_loss_pct
        # trailing активен после выхода в прибыль (hwm > entry), стоп от пика
        hit_trail = (order.trailing_pct is not None and hwm > entry
                     and price <= hwm * (1 - order.trailing_pct))
        if not (hit_tp or hit_sl or hit_trail):
            return

        # атомарный claim pending→exiting: только победитель шлёт sell (C4)
        if not self.store.claim_trigger(row["id"], "pending", "exiting"):
            return
        reason = "TP" if hit_tp else "SL" if hit_sl else "TRAIL"
        log.info("POSITION #%s %s %+.1f%% (пик %+.1f%%) → закрываю (%s)", row["id"],
                 order.token, change * 100, (hwm - entry) / entry * 100, reason)
        close = Order(wallet=order.wallet, side=Side.SELL, token=order.token,
                      type=OrderType.MARKET, sell_all=True,
                      slippage_pct=order.slippage_pct, tag=f"{order.tag}:{reason}",
                      client_order_id=f"{order.client_order_id}:exit" if order.client_order_id else "")
        on_sent = lambda txh: self.store.set_trigger_status(row["id"], "exiting", exit_tx=txh)
        res = self.ex.execute(close, on_sent=on_sent, confirm_secs=self.cfg.loop_confirm_secs)
        self._journal(close, res)
        if res.pending:
            log.warning("POSITION #%s выход не подтверждён (%s) — резолв позже", row["id"], res.tx_hash)
            self.store.set_trigger_status(row["id"], "exiting", exit_tx=res.tx_hash)
            return
        if res.ok:
            self.store.set_trigger_status(row["id"], "closed", exit_tx=res.tx_hash)
        else:
            # своп не прошёл (revert/ошибка) → назад в pending; _resolve_exiting/balance
            # не дадут продать дважды, если на деле баланс уже ушёл. B1: позицию НЕ
            # бросаем в failed — стоп должен жить и ретраиться, иначе токен без защиты.
            n = self.store.bump_attempts(row["id"])
            if n % ALERT_EVERY_FAILS == 0:
                log.error("POSITION #%s: %d неудачных выходов подряд (%s) — ВНИМАНИЕ, стоп не проходит",
                          row["id"], n, res.error)
            else:
                log.warning("POSITION #%s выход не прошёл (%s), попытка %d — назад в pending",
                            row["id"], res.error, n)
            self.store.set_trigger_status(row["id"], "pending")

    def _journal(self, order: Order, res) -> None:
        rec = {
            "ts": res.ts, "wallet": order.wallet, "token": order.token, "side": order.side.value,
            "venue": res.venue.value if res.venue else None, "tx_hash": res.tx_hash,
            "status": res.status.value if res.status else None, "filled_amount": res.filled_amount,
            "avg_price": res.avg_price, "tag": order.tag, "client_order_id": order.client_order_id,
        }
        self.store.record_fill(rec)
        if res.bscscan():
            log.info("fill %s %s → %s", order.side.value, order.token, res.bscscan())
