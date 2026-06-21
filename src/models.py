from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"   # исполнить сразу по текущей цене
    LIMIT = "limit"     # исполнить когда цена пересечёт trigger_price


class Venue(str, Enum):
    TWAK = "twak"
    DIRECT = "direct"


class OrderStatus(str, Enum):
    NEW = "new"            # принят, ещё не исполнен
    PENDING = "pending"    # лимит/условный — ждёт триггера
    EXITING = "exiting"    # своп отправлен, ждём подтверждения
    FILLED = "filled"      # исполнен и подтверждён ончейн
    CLOSED = "closed"      # позиция закрыта (TP/SL отработал)
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    """Команда от 'мозга'. Исполнитель не думает — только делает то, что тут описано.

    Цены везде во ВНУТРЕННЕЙ единице: BNB за 1 целый токен (то, что отдаёт
    getAmountsOut для 1 токена). Мозг должен слать trigger_price в этой же единице.
    """

    wallet: str                              # имя тела из конфига, напр. "body1"
    side: Side
    token: str                               # адрес ERC-20 токена на BSC
    type: OrderType = OrderType.MARKET
    amount: Optional[float] = None           # BUY: сколько BNB тратим; SELL: сколько токенов
    sell_all: bool = False                   # SELL: продать весь баланс токена
    trigger_price: Optional[float] = None    # LIMIT: BNB за 1 токен — порог входа
    take_profit_pct: Optional[float] = None  # напр. 0.20 = +20% (для BUY-позиции)
    stop_loss_pct: Optional[float] = None    # напр. 0.10 = -10%
    trailing_pct: Optional[float] = None     # напр. 0.08 = трейл-стоп 8% от пика
    slippage_pct: Optional[float] = None     # None → дефолт из конфига (пер-токен)
    client_order_id: str = ""                # ключ идемпотентности/отмены от мозга
    tag: str = ""                            # метка от мозга для логов

    def key(self) -> str:
        """Ключ дедупликации: client_order_id, иначе детерминированная подпись.

        Подпись включает ВСЕ экономически значимые поля — иначе две разные
        лимитки с одинаковыми wallet+amount+tag схлопывались в один ключ и вторая
        молча терялась. Мозг по-прежнему может слать client_order_id для явного
        контроля идемпотентности/отмены.
        """
        if self.client_order_id:
            return self.client_order_id

        # float-поля нормализуем: иначе 0.1+0.2 → "0.30000000000000004" != "0.3",
        # и тот же по смыслу ордер от мозга даёт другой ключ → дубль не распознан.
        def f(x):
            return format(x, ".10g") if isinstance(x, float) else str(x)
        # token лоуэркейсим: иначе один и тот же токен в разном регистре даёт разные ключи
        # → дубль не дедупится (двойная покупка).
        return (f"{self.wallet}:{self.side.value}:{self.token.lower()}:{self.type.value}:"
                f"{f(self.amount)}:{self.sell_all}:{f(self.trigger_price)}:"
                f"{f(self.take_profit_pct)}:{f(self.stop_loss_pct)}:{f(self.trailing_pct)}:{self.tag}")


@dataclass
class ExecResult:
    ok: bool
    venue: Optional[Venue] = None
    status: Optional[OrderStatus] = None
    tx_hash: Optional[str] = None
    filled_amount: Optional[float] = None    # BUY: получено токенов; SELL: получено BNB
    avg_price: Optional[float] = None        # BNB за 1 токен по факту филла
    gas_used: Optional[int] = None
    error: Optional[str] = None
    pending: bool = False                     # tx отправлена, но не подтверждена (таймаут) — НЕ ре-слать
    ts: float = field(default_factory=time.time)
    raw: dict = field(default_factory=dict)

    def bscscan(self) -> Optional[str]:
        return f"https://bscscan.com/tx/{self.tx_hash}" if self.tx_hash else None
