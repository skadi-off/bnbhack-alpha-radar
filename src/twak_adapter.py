from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from .config import Config, Wallet
from .models import ExecResult, Order, OrderStatus, Venue

log = logging.getLogger("twak")
TWAK_TIMEOUT = 90


class TwakError(Exception):
    pass


class TwakTimeout(TwakError):
    """twak-команда подвисла — своп МОГ уйти в сеть. Наверху НЕЛЬЗЯ фоллбэчить на direct
    (двойной своп), нужно резолвить по балансу (M5)."""


class TwakAdapter:
    """Путь через Trust Wallet Agent Kit CLI. Синтаксис сверен по trustwallet/tw-agent-skills.

    Реальные команды (фактчек):
      twak swap <amount> <from> <to> --chain bsc --slippage <pct> --json
    НЕТ `twak trade buy/sell`, НЕТ `--wallet`, НЕТ `--amount all`. Кошелёк — один на
    инстанс (~/.twak/wallet.json), пароль из env TWAK_WALLET_PASSWORD. Для 4 тел нужны
    4 отдельных HOME/конфиг-дира (передаём через env HOME на вызов) ИЛИ direct-путь.

    ВАЖНО: даже при ok возвращаем ok ТОЛЬКО если в --json есть подтверждённый txHash.
    Никакого «ok=True всегда» — это был баг скелета, маскировавший провалы.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        if shutil.which(cfg.twak_bin) is None:
            raise TwakError(f"twak бинарь не найден: {cfg.twak_bin}")

    def _env_for(self, wallet: Wallet) -> dict:
        env = dict(os.environ)
        # каждое тело — свой конфиг-дир ~/.twak; кладём в <HOME>/.twak-<name>
        home = os.path.join(os.path.expanduser("~"), f".twak-{wallet.name}")
        env["HOME"] = home
        return env

    def _run(self, wallet: Wallet, args: list[str]) -> dict:
        try:
            proc = subprocess.run(
                [self.cfg.twak_bin, *args, "--json"],
                capture_output=True, text=True, timeout=TWAK_TIMEOUT, env=self._env_for(wallet),
            )
        except subprocess.TimeoutExpired:
            # M5: команда не ответила за TWAK_TIMEOUT — своп мог уйти. Спец-исключение,
            # чтобы наверху НЕ фоллбэкнуть на direct (двойной своп).
            raise TwakTimeout(f"twak не ответил за {TWAK_TIMEOUT}s — статус свопа неизвестен")
        if proc.returncode != 0:
            raise TwakError(proc.stderr.strip() or proc.stdout.strip() or "twak ненулевой код")
        try:
            return json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            raise TwakError(f"twak вернул не-JSON: {proc.stdout[:200]}")

    def _swap(self, wallet: Wallet, order: Order, frm: str, to: str, amount: str) -> ExecResult:
        out = self._run(wallet, [
            "swap", amount, frm, to,
            "--chain", "bsc",
            "--slippage", str((order.slippage_pct or self.cfg.default_slippage_pct) * 100),
        ])
        txh = out.get("txHash") or out.get("tx_hash") or out.get("hash")
        if not txh:
            return ExecResult(ok=False, venue=Venue.TWAK, status=OrderStatus.FAILED,
                              error="twak swap без txHash — не считаем успехом", raw=out)
        return ExecResult(ok=True, venue=Venue.TWAK, status=OrderStatus.FILLED,
                          tx_hash=txh, filled_amount=out.get("toAmount"),
                          avg_price=out.get("price"), raw=out)

    def buy(self, wallet: Wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        # buy = swap <amount BNB> BNB <token>. twak синхронно отдаёт txHash, поэтому
        # окна «отправил-но-не-записал» нет — on_sent не нужен (принимаем для единого
        # интерфейса с DirectAdapter).
        res = self._swap(wallet, order, "BNB", order.token, str(order.amount))
        if on_sent is not None and res.tx_hash:
            try:
                on_sent(res.tx_hash)
            except Exception:
                pass
        return res

    def sell(self, wallet: Wallet, order: Order, on_sent=None, confirm_secs=None) -> ExecResult:
        # sell = обратный swap; 'all' нет → нужен явный amount (для sell_all юзаем direct)
        if order.sell_all or order.amount is None:
            return ExecResult(ok=False, venue=Venue.TWAK, status=OrderStatus.FAILED,
                              error="twak не умеет sell_all — уходим на direct")
        res = self._swap(wallet, order, order.token, "BNB", str(order.amount))
        if on_sent is not None and res.tx_hash:
            try:
                on_sent(res.tx_hash)
            except Exception:
                pass
        return res
