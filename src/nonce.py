from __future__ import annotations

import threading

from .rpc import RpcPool


class NonceManager:
    """Per-wallet nonce под локом.

    Зачем: get_transaction_count без 'pending' лагает, когда несколько tx уходят в
    одну секунду (buy + сразу sell по триггеру, или батч на одно тело) → 'nonce too
    low' / replacement underpriced, одна tx теряется. Держим монотонный счётчик на
    адрес, ресинкаем с цепочки при первом обращении и на ошибке.
    """

    def __init__(self, pool: RpcPool):
        self._pool = pool
        self._next: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, address: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(address, threading.Lock())

    def reserve(self, address: str) -> int:
        """Выдать следующий nonce для адреса (потокобезопасно)."""
        with self._lock_for(address):
            # nonce ОБЯЗАН читаться с primary — того же узла, через который уйдёт tx
            # (с реплики он лагает → коллизия/'nonce too low', K1).
            chain = self._pool.primary_call(
                lambda w3: w3.eth.get_transaction_count(address, "pending")
            )
            cur = self._next.get(address, 0)
            nonce = max(cur, chain)
            self._next[address] = nonce + 1
            return nonce

    def resync(self, address: str) -> None:
        """Сбросить локальный счётчик к цепочке (после ошибки/застрявшей tx)."""
        with self._lock_for(address):
            self._next.pop(address, None)

    def release(self, address: str, n: int) -> None:
        """Откатить РОВНО эту резервацию n (tx так и не отправлялась), не затирая весь
        счётчик. resync здесь опасен (MED3): при in-flight tx на том же коше он сбросил бы
        счётчик к цепочке → следующая tx взяла бы занятый nonce → коллизия/дроп."""
        with self._lock_for(address):
            if self._next.get(address) == n + 1:   # n был последним выданным
                self._next[address] = n

    def is_gap(self, address: str) -> bool:
        """C1-детектор nonce-дыры: локальный счётчик ушёл вперёд, а в мемпуле НЕТ
        незамайненных tx (pending==latest) → наша tx дропнулась, слот завис.

        Читаем СТРОГО с primary (того же узла, через который шлём). Безопасность от
        двойной траты обеспечивает не счётчик, а машина состояний (exiting не ре-файрит)
        + сам блокчейн (один nonce = максимум одна замайненная tx). Здесь только чиним
        зависание. Вызывать с K-тиковой выдержкой (см. daemon), чтобы не дёрнуть тx,
        которая ещё в полёте."""
        with self._lock_for(address):
            cur = self._next.get(address)
            if cur is None:
                return False
            pending = self._pool.primary_call(
                lambda w3: w3.eth.get_transaction_count(address, "pending"))
            latest = self._pool.primary_call(
                lambda w3: w3.eth.get_transaction_count(address, "latest"))
            return pending == latest and cur > pending
