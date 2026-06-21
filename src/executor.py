from __future__ import annotations

import logging
from typing import Optional

from .config import Config
from .direct_adapter import DirectAdapter
from .models import ExecResult, Order, OrderStatus, Side
from .nonce import NonceManager
from .rpc import RpcPool, TxTimeout
from .twak_adapter import TwakAdapter, TwakError, TwakTimeout

log = logging.getLogger("executor")


class Executor:
    """Слой исполнения на 4 тела. Исполняет ТОЛЬКО маркет-свопы (buy/sell).

    Лимит и условные ордера (TP/SL/trailing) держит движок триггеров — он зовёт
    этот execute(), когда цена пересекла порог. Сначала TWAK (если включён и не
    prefer_direct), при сбое — прямой путь.
    """

    def __init__(self, cfg: Config, pool: RpcPool, nonce: NonceManager, prefer_direct: bool = True):
        self.cfg = cfg
        self.pool = pool
        self.direct = DirectAdapter(cfg, pool, nonce)  # всегда: цены/балансы для триггеров
        # путь исполнения свопов: агрегатор (если задан) или тот же direct
        if cfg.aggregator == "openocean":
            from .openocean import OpenOceanAdapter
            self.exec_adapter = OpenOceanAdapter(cfg, pool, nonce)
            log.info("исполнение через агрегатор OpenOcean")
        else:
            self.exec_adapter = self.direct
        # По умолчанию prefer_direct=True: путь twak оставлен опциональным, т.к. его
        # модель «4 тела одним инстансом» неприменима (фактчек) и старый адаптер
        # рапортовал ложный успех. Включать twak осознанно, проверив синтаксис.
        self.prefer_direct = prefer_direct
        self.twak: Optional[TwakAdapter] = None
        if not prefer_direct:
            try:
                self.twak = TwakAdapter(cfg)
            except TwakError as e:
                log.warning("TWAK недоступен (%s) — только прямой путь", e)
        self._wallets = {w.name: w for w in cfg.wallets}

    def execute(self, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        """on_sent(txh): колбэк, вызываемый сразу после broadcast свопа (фиксация
        хэша в персисте до ожидания/крэша — см. DirectAdapter._send_and_confirm).
        confirm_secs: короткий таймаут подтверждения для live-цикла (#8); None → дефолт."""
        wallet = self._wallets.get(order.wallet)
        if wallet is None:
            return ExecResult(ok=False, status=OrderStatus.FAILED,
                              error=f"кошелёк {order.wallet} не найден в конфиге")

        # основной путь (twak), если включён
        if not self.prefer_direct and self.twak is not None:
            try:
                res = self._do(self.twak, wallet, order, on_sent, confirm_secs)
                if res.ok:
                    return res
                log.warning("TWAK not-ok (%s) → фоллбэк на прямой путь", res.error)
            except TwakTimeout as e:
                # M5: twak подвис — своп МОГ уйти. НЕ фоллбэчим на direct (был бы двойной
                # своп), отдаём pending → резолв по балансу (ничего не дублируем).
                log.warning("TWAK таймаут (%s) — НЕ фоллбэк, отдаю на резолв", e)
                return ExecResult(ok=False, status=OrderStatus.EXITING, pending=True,
                                  error="twak таймаут — статус свопа неизвестен, резолв по балансу")
            except Exception as e:
                log.warning("TWAK сбой (%s) → фоллбэк на прямой путь", e)

        # резервный/основной путь (direct или агрегатор)
        try:
            return self._do(self.exec_adapter, wallet, order, on_sent, confirm_secs)
        except TxTimeout as e:
            # tx ушла, но не подтвердилась за таймаут — может смайниться позже.
            # ok=False, но pending=True + tx_hash: вызывающий НЕ должен ре-слать,
            # резолв делается по receipt/балансу (см. TriggerEngine._resolve_exiting).
            log.warning("tx не подтверждена (таймаут): %s — оставляю на резолв", e.tx_hash)
            return ExecResult(ok=False, status=OrderStatus.EXITING, tx_hash=e.tx_hash,
                              pending=True, error="tx не подтверждена за таймаут (возможно в мемпуле)")
        except Exception as e:
            log.exception("прямой путь упал")
            return ExecResult(ok=False, status=OrderStatus.FAILED, error=str(e))

    @staticmethod
    def _do(adapter, wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        if order.side == Side.BUY:
            return adapter.buy(wallet, order, on_sent=on_sent, confirm_secs=confirm_secs)
        return adapter.sell(wallet, order, on_sent=on_sent, confirm_secs=confirm_secs)
