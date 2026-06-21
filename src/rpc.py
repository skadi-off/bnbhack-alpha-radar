from __future__ import annotations

import logging
import threading
import time
from typing import Callable, TypeVar

from web3 import Web3
from web3.exceptions import TransactionNotFound

# POA middleware: BSC требует его. Имя менялось между web3 v6 и v7.
try:
    from web3.middleware import geth_poa_middleware as _poa  # web3 v6
except ImportError:  # web3 v7
    from web3.middleware import ExtraDataToPOAMiddleware as _poa

log = logging.getLogger("rpc")

T = TypeVar("T")


class TxTimeout(Exception):
    """tx отправлена, но не подтверждена за таймаут — МОЖЕТ смайниться позже.

    Несёт tx_hash. Ловить отдельно: НЕ считать чистым провалом и НЕ ре-слать
    (иначе двойной расход), НЕ resync-ать nonce (наш nonce уже в мемпуле).
    """

    def __init__(self, tx_hash: str):
        self.tx_hash = tx_hash
        super().__init__(f"tx не подтверждена за таймаут (возможно в мемпуле): {tx_hash}")


class RpcPool:
    """Пул RPC-провайдеров с ротацией и ретраем.

    Урок OKX-фермы: «вчера работало, сегодня нет» → первой подозревать инфру,
    не код. Один публичный endpoint падает/лимитится — пропущенный poll = непойманный
    стоп/лимит. Поэтому read-вызовы оборачиваем в call() с ротацией и бэкоффом.

    primary (rpc_urls[0]) можно ставить private/MEV-protect — send_raw_transaction
    идёт всегда через primary, чтобы tx не светилась в публичном мемпуле.
    """

    def __init__(self, rpc_urls: list[str], timeout: int = 20):
        if not rpc_urls:
            raise ValueError("rpc_urls пуст")
        self._urls = rpc_urls
        self._providers: list[Web3] = []
        for url in rpc_urls:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
            try:
                w3.middleware_onion.inject(_poa, layer=0)
            except ValueError:
                pass  # уже добавлен
            except Exception as e:
                log.warning("POA-middleware не встал на %s: %s", url, e)
            self._providers.append(w3)
        self._idx = 0
        self._lock = threading.Lock()

    @property
    def primary(self) -> Web3:
        return self._providers[0]

    @property
    def w3(self) -> Web3:
        """Текущий активный провайдер (для построения контрактов и т.п.)."""
        return self._providers[self._idx]

    def call(self, fn: Callable[[Web3], T], retries: int = 4, base_delay: float = 0.4) -> T:
        """Выполнить read-операцию с ротацией по провайдерам и экспоненциальным бэкоффом.

        fn получает Web3 и должен быть idempotent (read-only). НЕ использовать для
        send_raw_transaction — слепой ретрай отправки = двойная tx (см. send()).
        """
        last: Exception | None = None
        n = len(self._providers)
        for attempt in range(retries):
            w3 = self._providers[(self._idx + attempt) % n]
            try:
                out = fn(w3)
                with self._lock:
                    self._idx = (self._idx + attempt) % n  # залипнуть на рабочем
                return out
            except Exception as e:  # сеть/таймаут/5xx/rate-limit
                last = e
                delay = base_delay * (2 ** attempt)
                log.warning("RPC сбой (%s), попытка %d/%d, ждём %.1fs",
                            e, attempt + 1, retries, delay)
                time.sleep(delay)
        raise RuntimeError(f"все RPC провалились после {retries} попыток: {last}")

    def primary_call(self, fn: Callable[[Web3], T], retries: int = 3, base_delay: float = 0.3) -> T:
        """Read СТРОГО через primary (тот же узел, через который шлём tx).

        Для всего, что влияет на КОРРЕКТНОСТЬ конкретной tx: preflight eth_call,
        estimate_gas, nonce, замер баланса для фила. Ротация тут опасна — отстающая
        реплика даёт ложный реверт/мусорный фил (K1). Ретраим тот же узел, НЕ ротируем.
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return fn(self.primary)
            except Exception as e:
                last = e
                time.sleep(base_delay * (2 ** attempt))
        raise RuntimeError(f"primary RPC провалился после {retries} попыток: {last}")

    def get_receipt_any(self, tx_hash: str):
        """Receipt по ЛЮБОЙ ноде (ротация), без блокировки. None если нигде нет.

        send идёт через primary (private-MEV 48.club), а он receipt по хэшу может НЕ
        отдавать → резолв выхода завис бы навечно (K2). Поэтому подтверждение ищем по
        всем нодам, включая публичные.
        """
        for w3 in self._providers:
            try:
                r = w3.eth.get_transaction_receipt(tx_hash)
                if r is not None:
                    return r
            except TransactionNotFound:
                continue
            except Exception:
                continue
        return None

    def send_raw(self, raw_tx) -> str:
        """Отправка подписанной tx через primary (никаких ретраев вслепую).

        Если primary отверг приватную tx — не падаем на публичный молча: это
        решение уровня выше (приватность важнее скорости включения).
        """
        # to_hex: на hexbytes>=1.0 HexBytes.hex() отдаёт строку БЕЗ префикса 0x,
        # из-за чего wait_for_transaction_receipt падал на каждой tx (ложный таймаут).
        return Web3.to_hex(self.primary.eth.send_raw_transaction(raw_tx))

    def wait_receipt(self, tx_hash: str, timeout: int, poll: float = 2.0):
        """Дождаться receipt, опрашивая по всем провайдерам; БЕЗ ре-броадкаста.

        Отличие от wait_for_transaction_receipt в pool.call: тот при таймауте
        ретраил весь wait по 4 раза (до 4×timeout) и отдавал generic-ошибку, на
        которой вызывающий ре-слал tx → двойной расход. Здесь при истечении
        времени бросаем TxTimeout(tx_hash) — отправитель уже знает хэш и НЕ ре-шлёт.
        'not mined yet' и сбой одного RPC не считаются провалом — просто ждём дальше.
        """
        end = time.time() + timeout
        n = len(self._providers)
        i = 0
        while time.time() < end:
            w3 = self._providers[i % n]
            i += 1
            try:
                r = w3.eth.get_transaction_receipt(tx_hash)
                if r is not None:
                    return r
            except TransactionNotFound:
                pass  # ещё не в блоке
            except Exception as e:  # сбой/лимит конкретного RPC — пробуем следующий
                log.debug("wait_receipt: RPC сбой (%s), следующий провайдер", e)
            # H3: не пересыпать за срок — спим не дольше остатка, иначе блокируем
            # однопоточный цикл демона дольше confirm_secs и проспим стопы других тел.
            remaining = end - time.time()
            if remaining <= 0:
                break
            time.sleep(min(poll, remaining))
        raise TxTimeout(tx_hash)
