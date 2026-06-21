from __future__ import annotations

import math

from .models import Order, OrderType, Side


def _num(d: dict, key):
    """Числовое поле → конечный НЕотрицательный float. None остаётся None.

    Закрывает класс крашей/дыр от мусора мозга:
    - строка '5.0' (иначе int('5.0'*1e18) string-repeat → OverflowError);
    - inf/nan (N2: amount=inf, slippage=nan проходит клэмп → int(nan) падает ПОСЛЕ approve;
      trigger=inf → лимитка срабатывает мгновенно; stop_loss=inf → позиция без стопа);
    - отрицательные (HIGH: slip=-0.5 → min_out=expected*1.5 → вечный реверт+слив газа;
      tp/sl<0 → инверсия). Все эти поля семантически >= 0."""
    v = d.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"поле {key}={v!r} не число")
    if not math.isfinite(f):
        raise ValueError(f"поле {key}={v!r} не конечное (inf/nan)")
    if f < 0:
        raise ValueError(f"поле {key}={v!r} отрицательное (должно быть >= 0)")
    return f


def order_from_dict(d: dict) -> Order:
    return Order(
        wallet=str(d["wallet"]),
        side=Side(d["side"]),
        token=str(d["token"]),
        type=OrderType(d.get("type", "market")),
        amount=_num(d, "amount"),
        sell_all=bool(d.get("sell_all", False)),
        trigger_price=_num(d, "trigger_price"),
        take_profit_pct=_num(d, "take_profit_pct"),
        stop_loss_pct=_num(d, "stop_loss_pct"),
        trailing_pct=_num(d, "trailing_pct"),
        slippage_pct=_num(d, "slippage_pct"),
        client_order_id=str(d.get("client_order_id", "")),
        tag=str(d.get("tag", "")),
    )


def order_to_dict(o: Order) -> dict:
    return {
        "wallet": o.wallet, "side": o.side.value, "token": o.token, "type": o.type.value,
        "amount": o.amount, "sell_all": o.sell_all, "trigger_price": o.trigger_price,
        "take_profit_pct": o.take_profit_pct, "stop_loss_pct": o.stop_loss_pct,
        "trailing_pct": o.trailing_pct, "slippage_pct": o.slippage_pct,
        "client_order_id": o.client_order_id, "tag": o.tag,
    }
