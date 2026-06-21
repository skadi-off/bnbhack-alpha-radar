from __future__ import annotations

"""Read-API для «мозга»: текущее состояние скелета одним JSON (read-only, ничего не шлёт).

    python -m src.status --config config.yaml [--wallet body1] [--fills 50]

Возвращает: на каждое тело — баланс BNB + открытые позиции (токен/entry/hwm/статус) с
текущей ценой и токен-балансом; активные лимитки; последние филлы (журнал сделок).
Мозг читает это, чтобы решать, что класть в inbox. Цены/балансы — с primary (как и tx)."""

import argparse
import json
import sys

from .config import load_config
from .direct_adapter import DirectAdapter
from .nonce import NonceManager
from .rpc import RpcPool
from .store import Store


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e)} if default is None else default


def build_status(cfg, fills_limit: int = 50, wallet_filter: str | None = None) -> dict:
    pool = RpcPool(cfg.rpc_urls)
    direct = DirectAdapter(cfg, pool, NonceManager(pool))
    store = Store(cfg.state_db)

    rows = list(store.active_triggers())
    price_cache: dict[str, float] = {}

    def price(token):
        if token not in price_cache:
            price_cache[token] = _safe(lambda: direct.price_in_bnb(token), default=None)
        return price_cache[token]

    bodies = []
    for w in cfg.wallets:
        if wallet_filter and w.name != wallet_filter:
            continue
        positions, limits = [], []
        for r in rows:
            if r["wallet"] != w.name:
                continue
            order_token = r["token"]
            common = {"id": r["id"], "token": order_token, "status": r["status"],
                      "side": r["side"], "client_order_id": r["client_order_id"],
                      "price_now": price(order_token)}
            if r["kind"] == "position":
                bal = _safe(lambda: direct.balance_token(order_token, w.address, primary=True), default=None)
                dec = _safe(lambda: direct.decimals(order_token), default=18)
                common.update(entry_price=r["entry_price"], hwm=r["hwm"],
                              token_balance=(bal / 10 ** dec) if isinstance(bal, int) else None)
                positions.append(common)
            elif r["kind"] == "limit":
                common["trigger_price"] = json.loads(r["order_json"]).get("trigger_price")
                limits.append(common)
        bodies.append({
            "wallet": w.name, "address": w.address,
            "bnb": _safe(lambda: direct.balance_bnb(w.address, primary=True) / 1e18, default=None),
            "positions": positions, "limits": limits,
        })

    fills = [dict(r) for r in store.recent_fills(fills_limit)]
    return {"bodies": bodies, "recent_fills": fills}


def main() -> None:
    ap = argparse.ArgumentParser(description="twak_executor read-API (состояние для мозга)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--wallet", default=None, help="фильтр по имени тела")
    ap.add_argument("--fills", type=int, default=50)
    args = ap.parse_args()
    cfg = load_config(args.config)
    json.dump(build_status(cfg, args.fills, args.wallet), sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
