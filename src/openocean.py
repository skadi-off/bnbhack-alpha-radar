from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from web3 import Web3

from .config import Config, is_token_allowed, slippage_for
from .direct_adapter import DirectAdapter
from .models import ExecResult, Order, OrderStatus, Venue
from .nonce import NonceManager
from .rpc import RpcPool

log = logging.getLogger("openocean")

# ОПЦИОНАЛЬНЫЙ путь: агрегатор OpenOcean v4 (BSC, без ключа) отдаёт готовый calldata с
# лучшим роутингом V2/V3/stable. Включается cfg.aggregator == "openocean".
# ВНИМАНИЕ: против live-API не протестировано — гонять сперва через --dry-run/малую сумму.
BASE = "https://open-api.openocean.finance/v4/bsc"
NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"  # BNB в OO


def _get(path: str, params: dict) -> dict:
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.loads(r.read())
    if str(data.get("code")) not in ("200", "0"):
        raise RuntimeError(f"OpenOcean {path}: {data.get('message') or data}")
    return data["data"]


class OpenOceanAdapter:
    """Исполнение через OpenOcean-агрегатор. Тот же интерфейс buy/sell, что у DirectAdapter."""

    def __init__(self, cfg: Config, pool: RpcPool, nonce: NonceManager):
        self.cfg = cfg
        self.pool = pool
        self.nonce = nonce
        self.direct = DirectAdapter(cfg, pool, nonce)  # для allowance/receipt/баланса

    def _swap(self, wallet, order: Order, in_token: str, out_token: str, amount_human: str,
              on_sent=None, confirm_secs=None) -> ExecResult:
        acct = self.pool.primary.eth.account.from_key(wallet.private_key)
        slip = slippage_for(self.cfg.params_for(wallet.name), order.token, order.slippage_pct,
                            self.cfg.max_slippage_pct)
        gas_price_gwei = (self.direct._gas_price()) / 1e9
        data = _get("swap", {
            "inTokenAddress": in_token, "outTokenAddress": out_token,
            "amount": amount_human, "gasPrice": gas_price_gwei,
            "slippage": slip * 100, "account": acct.address,
        })
        spender = Web3.to_checksum_address(data["to"])
        tx = {
            "from": acct.address,
            "to": spender,
            "data": data["data"],
            "value": int(data.get("value", 0)),
        }
        # ERC-20 in → approve именно СПЕНДЕРУ OpenOcean (data["to"]), а не Pancake-роутеру.
        if in_token != NATIVE:
            dec = self.direct.decimals(in_token)
            self.direct._ensure_allowance(wallet, in_token,
                                          int(float(amount_human) * 10 ** dec), spender=spender,
                                          confirm_secs=confirm_secs)
        sent = self.direct._send_and_confirm(wallet, tx, on_sent=on_sent, confirm_secs=confirm_secs)
        return ExecResult(ok=True, venue=Venue.DIRECT, status=OrderStatus.FILLED,
                          tx_hash=sent["tx_hash"], gas_used=sent["receipt"].get("gasUsed"),
                          raw={"aggregator": "openocean"})

    def buy(self, wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        if not is_token_allowed(self.cfg.params_for(wallet.name), order.token):
            return ExecResult(ok=False, status=OrderStatus.FAILED, error="токен не в вайтлисте тела")
        return self._swap(wallet, order, NATIVE, order.token, str(order.amount),
                          on_sent=on_sent, confirm_secs=confirm_secs)

    def sell(self, wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        if not is_token_allowed(self.cfg.params_for(wallet.name), order.token):
            return ExecResult(ok=False, status=OrderStatus.FAILED, error="токен не в вайтлисте тела")
        if order.sell_all or order.amount is None:
            dec = self.direct.decimals(order.token)
            amt = self.direct.balance_token(order.token, wallet.address) / 10 ** dec
        else:
            amt = order.amount
        return self._swap(wallet, order, order.token, NATIVE, str(amt),
                          on_sent=on_sent, confirm_secs=confirm_secs)
