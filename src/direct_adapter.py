from __future__ import annotations

import logging
import time

from web3 import Web3

from .config import Config, Wallet, is_token_allowed, slippage_for
from .models import ExecResult, Order, OrderStatus, Venue
from .nonce import NonceManager
from .rpc import RpcPool, TxTimeout

log = logging.getLogger("direct")

ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactETHForTokensSupportingFeeOnTransferTokens", "type": "function",
     "stateMutability": "payable",
     "inputs": [{"name": "amountOutMin", "type": "uint256"}, {"name": "path", "type": "address[]"},
                {"name": "to", "type": "address"}, {"name": "deadline", "type": "uint256"}],
     "outputs": []},
    {"name": "swapExactTokensForETHSupportingFeeOnTransferTokens", "type": "function",
     "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"},
                {"name": "path", "type": "address[]"}, {"name": "to", "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": []},
]

ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]

MAX_UINT = 2 ** 256 - 1
# N5: продажа на меньше этого по выходу (BNB-wei) = пыль; не свопаем, закрываем позицию,
# чтобы не зациклить ретрай на остатке/пыли. ~0.00001 BNB.
DUST_OUT_WEI = 10 ** 13

# ---- PancakeSwap V3 ----
# Многие токены вайтлиста ликвидны ТОЛЬКО в V3 (или в V3 на порядки глубже) — без этого
# ~половину списка не купить/продать по нормальной цене. Котировка через QuoterV2,
# исполнение через V3 SwapRouter (exactInputSingle). Адреса канонические BSC.
V3_FEES = [100, 500, 2500, 10000]   # тиры комиссии пулов Pancake V3

QUOTER_ABI = [
    {"name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}], "name": "params", "type": "tuple"}],
     "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "a", "type": "uint160"},
                 {"name": "b", "type": "uint32"}, {"name": "c", "type": "uint256"}]},
]
V3_ROUTER_ABI = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
         {"name": "deadline", "type": "uint256"}, {"name": "amountIn", "type": "uint256"},
         {"name": "amountOutMinimum", "type": "uint256"}, {"name": "sqrtPriceLimitX96", "type": "uint160"}],
         "name": "params", "type": "tuple"}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]
WBNB_ABI = [
    {"name": "withdraw", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []},
]


class DirectAdapter:
    """Прямой путь: свопы на PancakeSwap V2 через BSC RPC, подпись локально.

    Отличия от скелета:
    - ждём receipt и проверяем status==1 (тихий реверт ≠ успех — урок OKX-фермы);
    - filled/avg_price из реальной дельты баланса, не из expected;
    - мультихоп через USDT, если прямой пул token↔WBNB отсутствует (BSB-кейс #9);
    - проверка баланса BNB + газ-резерв перед buy;
    - eth_call preflight перед отправкой (ловит реверт бесплатно);
    - газ max(oracle*mult, floor); nonce через NonceManager.
    """

    def __init__(self, cfg: Config, pool: RpcPool, nonce: NonceManager):
        self.cfg = cfg
        self.pool = pool
        self.nonce = nonce
        self.router_addr = Web3.to_checksum_address(cfg.router)
        self.wbnb = Web3.to_checksum_address(cfg.wbnb)
        self.usdt = Web3.to_checksum_address(cfg.usdt)
        self.v3_router = Web3.to_checksum_address(cfg.v3_router)
        self.v3_quoter = Web3.to_checksum_address(cfg.v3_quoter)

    # ---------- контракты (rotation-safe: строим внутри pool.call) ----------
    def _router_call(self, fn_name, *args):
        return self.pool.call(
            lambda w3: w3.eth.contract(address=self.router_addr, abi=ROUTER_ABI)
            .functions[fn_name](*args).call()
        )

    def _erc20_call(self, token: str, fn_name, *args, primary: bool = False):
        addr = Web3.to_checksum_address(token)
        fn = lambda w3: w3.eth.contract(address=addr, abi=ERC20_ABI).functions[fn_name](*args).call()
        # primary=True для замеров баланса под фил (K1): before/after с того же узла,
        # через который ушла tx — иначе дельта врёт.
        return self.pool.primary_call(fn) if primary else self.pool.call(fn)

    def decimals(self, token: str) -> int:
        return self._erc20_call(token, "decimals")

    def balance_token(self, token: str, owner: str, primary: bool = False) -> int:
        return self._erc20_call(token, "balanceOf", Web3.to_checksum_address(owner), primary=primary)

    def balance_bnb(self, owner: str, primary: bool = False) -> int:
        addr = Web3.to_checksum_address(owner)
        fn = lambda w3: w3.eth.get_balance(addr)
        return self.pool.primary_call(fn) if primary else self.pool.call(fn)

    # ---------- роутинг / котировки ----------
    def _candidate_paths(self, token: str, buy: bool) -> list[list[str]]:
        t = Web3.to_checksum_address(token)
        if buy:
            paths = [[self.wbnb, t]]
            if t != self.usdt:
                paths.append([self.wbnb, self.usdt, t])
        else:
            paths = [[t, self.wbnb]]
            if t != self.usdt:
                paths.append([t, self.usdt, self.wbnb])
        return paths

    def best_path(self, token: str, amount_in_wei: int, buy: bool) -> tuple[list[str], int]:
        """Вернуть (path, amount_out) с максимальным выходом среди прямого и мультихоп.

        Если ни один пул не котируется — RuntimeError с внятным текстом (не «неликвид»
        из V2-only проверки, а «нет пути token↔WBNB/USDT на роутере»).
        """
        best: tuple[list[str], int] | None = None
        for path in self._candidate_paths(token, buy):
            try:
                out = self._router_call("getAmountsOut", amount_in_wei, path)[-1]
            except Exception:
                continue
            if out > 0 and (best is None or out > best[1]):
                best = (path, out)
        if best is None:
            raise RuntimeError(
                f"нет ликвидного пути для {token} на роутере {self.cfg.router} "
                f"(проверены прямой и через USDT)"
            )
        return best

    def _v3_best(self, token: str, amount_in_wei: int, buy: bool) -> tuple:
        """Лучший single-hop V3-пул token↔WBNB по тирам. Вернуть (fee, out) или (None, 0).
        Котировка через QuoterV2 (eth_call). Если V3 выключен/нет пула — (None, 0)."""
        if not self.cfg.enable_v3:
            return (None, 0)
        t = Web3.to_checksum_address(token)
        token_in, token_out = (self.wbnb, t) if buy else (t, self.wbnb)
        best_fee, best_out = None, 0
        # #6: ротация по нодам, но БЕЗ ретрая ревертов. Реверт (нет пула с этим тиром) —
        # детерминирован, одинаков на всех нодах → к след. тиру. Сетевой сбой ноды → след. нода
        # (устойчивость к хиккапу primary, иначе V3-only токен молча теряет оценку стопа).
        for fee in V3_FEES:
            for w3 in self.pool._providers:
                try:
                    out = (w3.eth.contract(address=self.v3_quoter, abi=QUOTER_ABI)
                           .functions.quoteExactInputSingle((token_in, token_out, amount_in_wei, fee, 0)).call()[0])
                    if out > best_out:
                        best_fee, best_out = fee, out
                    break  # успех на этой ноде → к следующему тиру
                except Exception as e:
                    if "revert" in str(e).lower():
                        break  # пула с этим тиром нет — к след. тиру (на др. нодах то же)
                    continue  # сетевой сбой конкретной ноды → пробуем следующую
        return (best_fee, best_out)

    def best_route(self, token: str, amount_in_wei: int, buy: bool) -> dict:
        """Выбрать ЛУЧШИЙ маршрут между V2 (мультихоп) и V3 (single-hop) по выходу.
        Вернуть {venue:'v2'|'v3', out, path?(v2), fee?(v3)}. RuntimeError если нигде нет."""
        v2_path, v2_out = None, 0
        try:
            v2_path, v2_out = self.best_path(token, amount_in_wei, buy)
        except Exception:
            pass
        v3_fee, v3_out = self._v3_best(token, amount_in_wei, buy)
        if v2_out == 0 and v3_out == 0:
            raise RuntimeError(f"нет ликвидного пути для {token} (ни V2, ни V3)")
        if v3_out > v2_out:
            return {"venue": "v3", "out": v3_out, "fee": v3_fee}
        return {"venue": "v2", "out": v2_out, "path": v2_path}

    def price_in_bnb(self, token: str) -> float:
        """Цена: BNB за 1 целый токен (для движка триггеров). Лучшее из V2/V3."""
        d = self.decimals(token)
        return self.best_route(token, 10 ** d, buy=False)["out"] / 1e18

    # ---------- газ / отправка ----------
    def _gas_price(self) -> int:
        if self.cfg.gas_price_gwei:
            return Web3.to_wei(self.cfg.gas_price_gwei, "gwei")
        oracle = self.pool.call(lambda w3: w3.eth.gas_price)
        floor = Web3.to_wei(self.cfg.gas_floor_gwei, "gwei")
        return max(int(oracle * self.cfg.gas_multiplier), floor)

    def _send_and_confirm(self, wallet: Wallet, tx: dict, on_sent=None, confirm_secs=None) -> dict:
        """Подписать, preflight, отправить, дождаться receipt. Бросает при реверте.

        on_sent(txh): вызывается СРАЗУ после успешного broadcast (до ожидания
        receipt). Нужен, чтобы зафиксировать хэш в персисте до возможного
        крэша/таймаута — иначе резолв не сможет отличить «своп ушёл» от «не ушёл»
        и рискует ре-слать (двойной расход).
        """
        acct = self.pool.primary.eth.account.from_key(wallet.private_key)
        tx.setdefault("chainId", self.cfg.chain_id)
        # BSC — legacy gas (type 0). build_transaction на web3 v7 по умолчанию
        # проставляет EIP-1559 поля; вместе с gasPrice нода реверзит tx
        # ('both gasPrice and maxFeePerGas specified'). Убираем 1559, ставим legacy.
        tx.pop("maxFeePerGas", None)
        tx.pop("maxPriorityFeePerGas", None)
        tx.pop("type", None)
        tx.setdefault("gasPrice", self._gas_price())
        tx["nonce"] = self.nonce.reserve(acct.address)
        if "gas" not in tx:
            try:
                # estimate/preflight — СТРОГО primary (K1): на отстающей реплике дают
                # ложный реверт (особенно сразу после approve, ещё не видного реплике).
                est = self.pool.primary_call(lambda w3: w3.eth.estimate_gas(tx))
                tx["gas"] = int(est * 1.25)
            except Exception as e:
                # estimate реверзит = вероятный реверт самой tx; для FoT-токенов бывает
                # ложно, поэтому не отказываем, но логируем явно (а не молча).
                log.warning("estimate_gas упал (%s) → fallback %d", e, self.cfg.gas_limit_fallback)
                tx["gas"] = self.cfg.gas_limit_fallback
        # preflight: бесплатно ловим реверт по текущему состоянию (на primary, K1)
        try:
            self.pool.primary_call(lambda w3: w3.eth.call(tx))
        except Exception as e:
            self.nonce.release(acct.address, tx["nonce"])   # MED3: tx не ушла — откат своего nonce, не весь счётчик
            raise RuntimeError(f"preflight реверт: {e}")

        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        # хэш считается локально (keccak подписанной tx) — он валиден ещё ДО ответа ноды.
        local_hash = Web3.to_hex(Web3.keccak(raw))
        try:
            txh = self.pool.send_raw(raw)
        except Exception as e:
            # M1: различаем «tx ТОЧНО не ушла» от «могла уйти». Узел отверг по содержимому
            # (нет газа/низкий nonce/дешёвый газ) → tx НЕ в мемпуле, слот свободен → жёсткий
            # фейл + resync (иначе #4 пометил бы pending и стоп тыкался бы вечно по TTL).
            msg = str(e).lower()
            DEFINITE_NOT_SENT = ("insufficient funds", "insufficient balance", "nonce too low",
                                 "intrinsic gas", "gas required exceeds", "exceeds block gas limit",
                                 "transaction underpriced", "fee cap less", "max priority fee")
            # «already known»/«replacement» = tx уже в мемпуле → НЕ not-sent.
            if any(s in msg for s in DEFINITE_NOT_SENT) and "replacement" not in msg:
                self.nonce.release(acct.address, tx["nonce"])   # MED3: точечный откат, не весь счётчик
                raise RuntimeError(f"tx отвергнута узлом (не отправлена): {e}")
            # #4: иначе ответ мог оборваться при уже принятой tx → НЕ ресинкаем, отдаём как
            # pending (TxTimeout) → резолв по receipt/балансу. Перешлём только если докажем, что не прошло.
            log.warning("send_raw оборвался (%s) — отдаю %s на резолв (НЕ ре-слать)", e, local_hash)
            if on_sent is not None:
                try:
                    on_sent(local_hash)
                except Exception:
                    pass
            raise TxTimeout(local_hash)
        if on_sent is not None:
            try:
                on_sent(txh)  # зафиксировать хэш в персисте ДО ожидания/возможного крэша
            except Exception as e:
                log.warning("on_sent колбэк упал (%s) — продолжаем ожидание", e)
        # wait_receipt бросает TxTimeout(txh) при истечении срока — НЕ ресинкаем
        # nonce и НЕ считаем провалом (tx может смайниться). Пробрасываем наверх.
        receipt = self.pool.wait_receipt(txh, confirm_secs or self.cfg.receipt_timeout_secs)
        if receipt.get("status") != 1:
            self.nonce.resync(acct.address)
            raise RuntimeError(f"tx реверзнулась on-chain: {txh}")
        return {"tx_hash": txh, "receipt": receipt}

    def _ensure_allowance(self, wallet: Wallet, token: str, amount: int, spender=None,
                          confirm_secs=None) -> None:
        spender = Web3.to_checksum_address(spender) if spender else self.router_addr
        acct = self.pool.primary.eth.account.from_key(wallet.private_key)
        # H4: allowance читаем с primary — отстающая реплика не увидит свежий approve и
        # заставит слать ЛИШНИЙ approve-tx, который зря резервирует nonce (кормит C1).
        cur = self._erc20_call(token, "allowance", acct.address, spender, primary=True)
        if cur >= amount:
            return
        approve_amt = MAX_UINT if self.cfg.approve_mode == "infinite" else amount
        tx = (self.pool.primary.eth.contract(
            address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            .functions.approve(spender, approve_amt)
            .build_transaction({"from": acct.address}))
        tx.pop("nonce", None)
        tx.pop("gas", None)
        # H3: approve ждём с тем же коротким сроком, что и своп (confirm_secs), а НЕ дефолтные
        # 180с — иначе первый approve по телу блокирует весь однопоточный цикл и стопы других
        # тел не поллятся. #6: его TxTimeout НЕ даём всплыть как pending (иначе хэш approve
        # запишется в exit_tx свопа и позиция ложно закроется) — жёсткий фейл, повторим позже.
        try:
            self._send_and_confirm(wallet, tx, confirm_secs=confirm_secs)
        except TxTimeout as e:
            raise RuntimeError(f"approve не подтверждён ({e.tx_hash}) — продажа отложена")

    # ---------- действия ----------
    def buy(self, wallet: Wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        p = self.cfg.params_for(wallet.name)
        if not is_token_allowed(p, order.token):
            return ExecResult(ok=False, status=OrderStatus.FAILED,
                              error=f"токен {order.token} не в вайтлисте тела {wallet.name}")
        if not order.amount:
            return ExecResult(ok=False, status=OrderStatus.FAILED, error="buy: не задан amount (BNB)")
        acct = self.pool.primary.eth.account.from_key(wallet.private_key)
        value = Web3.to_wei(order.amount, "ether")

        # газ-резерв: не уходить в ноль по BNB (урок фермы — оставлять на газ выходов).
        # M1: считаем по 1.7× fallback-лимита — реальный FoT-своп может стоить дороже 350k.
        gas_price = self._gas_price()
        gas_cost = gas_price * int(self.cfg.gas_limit_fallback * 1.7)
        reserve = Web3.to_wei(p.bnb_gas_reserve, "ether")
        bal = self.balance_bnb(acct.address, primary=True)   # LOW: с primary, не с реплики
        if bal < value + gas_cost + reserve:
            return ExecResult(ok=False, status=OrderStatus.FAILED,
                              error=f"мало BNB: есть {bal/1e18:.5f}, надо ≥"
                                    f"{(value+gas_cost+reserve)/1e18:.5f} (+резерв)")

        route = self.best_route(order.token, value, buy=True)
        slip = slippage_for(p, order.token, order.slippage_pct, self.cfg.max_slippage_pct)
        min_out = int(route["out"] * (1 - slip))
        if min_out <= 0:
            return ExecResult(ok=False, status=OrderStatus.FAILED, error="buy: min_out == 0")
        deadline = int(time.time()) + self.cfg.deadline_secs
        tok = Web3.to_checksum_address(order.token)

        bal_before = self.balance_token(order.token, acct.address, primary=True)
        if route["venue"] == "v3":
            # V3: exactInputSingle{value} — роутер сам оборачивает BNB→WBNB, токен идёт нам.
            params = (self.wbnb, tok, route["fee"], acct.address, deadline, value, min_out, 0)
            tx = (self.pool.primary.eth.contract(address=self.v3_router, abi=V3_ROUTER_ABI)
                  .functions.exactInputSingle(params)
                  .build_transaction({"from": acct.address, "value": value}))
        else:
            tx = (self.pool.primary.eth.contract(address=self.router_addr, abi=ROUTER_ABI)
                  .functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                      min_out, [Web3.to_checksum_address(a) for a in route["path"]], acct.address, deadline)
                  .build_transaction({"from": acct.address, "value": value}))
        tx.pop("nonce", None)
        tx.pop("gas", None)
        sent = self._send_and_confirm(wallet, tx, on_sent=on_sent, confirm_secs=confirm_secs)

        received = self.balance_token(order.token, acct.address, primary=True) - bal_before
        d = self.decimals(order.token)
        tokens = received / 10 ** d
        avg = (order.amount / tokens) if tokens > 0 else None
        return ExecResult(ok=True, venue=Venue.DIRECT, status=OrderStatus.FILLED,
                          tx_hash=sent["tx_hash"], filled_amount=tokens, avg_price=avg,
                          gas_used=sent["receipt"].get("gasUsed"),
                          raw={"venue": route["venue"], "min_out": min_out, "expected": route["out"]})

    def sell(self, wallet: Wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        p = self.cfg.params_for(wallet.name)
        if not is_token_allowed(p, order.token):
            return ExecResult(ok=False, status=OrderStatus.FAILED,
                              error=f"токен {order.token} не в вайтлисте тела {wallet.name}")
        acct = self.pool.primary.eth.account.from_key(wallet.private_key)
        d = self.decimals(order.token)
        if order.sell_all or order.amount is None:
            amount_wei = self.balance_token(order.token, acct.address, primary=True)
        else:
            amount_wei = int(order.amount * 10 ** d)
        if amount_wei <= 0:
            return ExecResult(ok=False, status=OrderStatus.FAILED, error="sell: нулевой баланс/amount")

        # M1: газ-резерв-чек (в buy был, в sell не было). Нет BNB на газ → чистый FAILED, а не
        # бесконечный pending/send-revert. Когда тело дофондят — продажа пройдёт на след. тике.
        gas_cost = self._gas_price() * int(self.cfg.gas_limit_fallback * 1.7)
        reserve = Web3.to_wei(p.bnb_gas_reserve, "ether")
        if self.balance_bnb(acct.address, primary=True) < gas_cost + reserve:
            return ExecResult(ok=False, status=OrderStatus.FAILED,
                              error="sell: мало BNB на газ (нужно дофондить тело)")

        route = self.best_route(order.token, amount_wei, buy=False)
        expected = route["out"]
        # N5: остаток — пыль (выход меньше порога). Не свопаем и не ретраим вечно: считаем
        # позицию закрытой (ok), на пыль не тратим газ. Аллованс тоже не трогаем.
        if expected < DUST_OUT_WEI:
            log.info("sell %s: остаток пыль (выход %d wei < %d) — закрываю без свопа",
                     order.token, expected, DUST_OUT_WEI)
            return ExecResult(ok=True, venue=Venue.DIRECT, status=OrderStatus.CLOSED,
                              filled_amount=0.0, raw={"dust": True, "amount_wei": amount_wei})
        slip = slippage_for(p, order.token, order.slippage_pct, self.cfg.max_slippage_pct)
        min_out = int(expected * (1 - slip))
        if min_out <= 0:
            return ExecResult(ok=False, status=OrderStatus.FAILED, error="sell: min_out == 0")
        deadline = int(time.time()) + self.cfg.deadline_secs
        tok = Web3.to_checksum_address(order.token)

        if route["venue"] == "v3":
            # V3: approve СПЕНДЕРУ V3-роутера; своп token→WBNB на наш адрес; потом unwrap WBNB→BNB.
            self._ensure_allowance(wallet, order.token, amount_wei, spender=self.v3_router,
                                   confirm_secs=confirm_secs)
            wbnb_before = self.balance_token(self.wbnb, acct.address, primary=True)
            params = (tok, self.wbnb, route["fee"], acct.address, deadline, amount_wei, min_out, 0)
            tx = (self.pool.primary.eth.contract(address=self.v3_router, abi=V3_ROUTER_ABI)
                  .functions.exactInputSingle(params).build_transaction({"from": acct.address}))
            tx.pop("nonce", None); tx.pop("gas", None)
            sent = self._send_and_confirm(wallet, tx, on_sent=on_sent, confirm_secs=confirm_secs)
            wbnb_recv = self.balance_token(self.wbnb, acct.address, primary=True) - wbnb_before
            # #5: своп уже прошёл (токен→WBNB). unwrap — косметика; его таймаут НЕ должен перетереть
            # swap-хэш и зациклить. Делаем best-effort: упал — лог, продажа всё равно ok по swap-хэшу.
            if wbnb_recv > 0:
                try:
                    wtx = (self.pool.primary.eth.contract(address=self.wbnb, abi=WBNB_ABI)
                           .functions.withdraw(wbnb_recv).build_transaction({"from": acct.address}))
                    wtx.pop("nonce", None); wtx.pop("gas", None)
                    self._send_and_confirm(wallet, wtx, confirm_secs=confirm_secs)
                except Exception as e:
                    log.warning("V3 sell %s: unwrap WBNB не прошёл (%s) — WBNB на коше, продажа ok",
                                order.token, e)
            bnb = wbnb_recv / 1e18
            tx_hash = sent["tx_hash"]; gas_used = sent["receipt"].get("gasUsed")
        else:
            self._ensure_allowance(wallet, order.token, amount_wei, confirm_secs=confirm_secs)
            # #1: bnb_before снимаем ПОСЛЕ approve — иначе газ approve вычтется из выручки.
            bnb_before = self.balance_bnb(acct.address, primary=True)
            tx = (self.pool.primary.eth.contract(address=self.router_addr, abi=ROUTER_ABI)
                  .functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                      amount_wei, min_out, [Web3.to_checksum_address(a) for a in route["path"]],
                      acct.address, deadline)
                  .build_transaction({"from": acct.address}))
            tx.pop("nonce", None); tx.pop("gas", None)
            sent = self._send_and_confirm(wallet, tx, on_sent=on_sent, confirm_secs=confirm_secs)
            receipt = sent["receipt"]
            gas_paid = receipt.get("gasUsed", 0) * receipt.get("effectiveGasPrice", self._gas_price())
            bnb = ((self.balance_bnb(acct.address, primary=True) - bnb_before) + gas_paid) / 1e18
            tx_hash = sent["tx_hash"]; gas_used = receipt.get("gasUsed")

        tokens = amount_wei / 10 ** d
        avg = (bnb / tokens) if tokens > 0 else None
        return ExecResult(ok=True, venue=Venue.DIRECT, status=OrderStatus.FILLED,
                          tx_hash=tx_hash, filled_amount=bnb, avg_price=avg, gas_used=gas_used,
                          raw={"venue": route["venue"], "amount_wei": amount_wei, "min_out": min_out})
