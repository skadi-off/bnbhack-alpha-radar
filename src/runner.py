from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config, slippage_for
from .executor import Executor
from .models import OrderType, Side
from .nonce import NonceManager
from .parse import order_from_dict
from .rpc import RpcPool

log = logging.getLogger("runner")


def _dry_run(cfg, pool, order) -> dict:
    """Собрать котировку и план без отправки tx (eth_call/quote), ничего не тратя."""
    from .direct_adapter import DirectAdapter
    from .nonce import NonceManager as _NM
    d = DirectAdapter(cfg, pool, _NM(pool))
    buy = order.side == Side.BUY
    from web3 import Web3
    if buy:
        amount_in = Web3.to_wei(order.amount or 0, "ether")
    else:
        dec = d.decimals(order.token)
        amount_in = int((order.amount or 0) * 10 ** dec)
    route = d.best_route(order.token, amount_in, buy=buy)  # как в бою: V2/V3 по лучшему выходу
    out = route["out"]
    slip = slippage_for(cfg.params_for(order.wallet), order.token, order.slippage_pct,
                        cfg.max_slippage_pct)
    return {
        "dry_run": True, "side": order.side.value, "token": order.token,
        "type": order.type.value, "venue": route["venue"],
        "path": route.get("path"), "v3_fee": route.get("fee"), "amount_in_wei": amount_in,
        "expected_out_wei": out, "min_out_wei": int(out * (1 - slip)), "slippage": slip,
        "price_bnb_per_token": d.price_in_bnb(order.token),
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="twak_executor — одноразовый прогон ордера(ов)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--order", default="-", help="JSON-файл с ордером(ами), или '-' для stdin")
    ap.add_argument("--use-twak", action="store_true", help="включить путь twak (по умолч. только direct)")
    ap.add_argument("--dry-run", action="store_true", help="только котировка/план, без отправки tx")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pool = RpcPool(cfg.rpc_urls)
    ex = None if args.dry_run else Executor(cfg, pool, NonceManager(pool), prefer_direct=not args.use_twak)

    src = sys.stdin if args.order == "-" else open(args.order)
    payload = json.load(src)
    orders = payload if isinstance(payload, list) else [payload]

    for od in orders:
        try:
            order = order_from_dict(od)
            if args.dry_run:
                out = _dry_run(cfg, pool, order)
            elif order.type == OrderType.LIMIT:
                out = {"ok": False, "error": "лимит/условные ордера — только через daemon (src.daemon)",
                       "hint": "положи ордер в inbox-папку демона"}
            else:
                res = ex.execute(order)
                out = {"ok": res.ok, "status": res.status.value if res.status else None,
                       "tx_hash": res.tx_hash, "bscscan": res.bscscan(),
                       "filled_amount": res.filled_amount, "avg_price": res.avg_price,
                       "error": res.error, "tag": order.tag}
        except Exception as e:  # один кривой ордер не роняет весь батч
            log.exception("ордер упал")
            out = {"ok": False, "error": str(e), "order": od}
        print(json.dumps(out, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
