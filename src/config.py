from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

log = logging.getLogger("config")

# Ключи, которые тело может переопределить в своей секции strategy: (пер-тело «мозг»/числа).
# Всё остальное (сеть, газ-флор, роутер, поллинг) — общее, инфраструктурное.
STRATEGY_KEYS = (
    "default_slippage_pct", "slippage_by_token", "bnb_gas_reserve",
    "take_profit_pct", "stop_loss_pct", "trailing_pct",
    "allowed_tokens", "max_drawdown_pct", "min_trades_per_day",
)


@dataclass
class Wallet:
    name: str
    address: str
    # M3: repr=False — иначе приватка утекает в любой repr(Wallet)/repr(Config) и в
    # log.exception, который печатает аргументы → слил бы 4 ключа. Сводит на нет keystore.
    private_key: str = field(repr=False)
    strategy: dict = field(default_factory=dict)  # пер-тело оверрайды (см. STRATEGY_KEYS)

    def __repr__(self) -> str:                     # явная гарантия: ключ никогда не печатаем
        return f"Wallet(name={self.name!r}, address={self.address!r}, private_key=***)"


@dataclass
class WalletParams:
    """Эффективные стратегические числа КОНКРЕТНОГО тела (глобальный дефолт ⊕ оверрайд)."""
    default_slippage_pct: float
    slippage_by_token: dict[str, float]
    bnb_gas_reserve: float
    take_profit_pct: Optional[float]
    stop_loss_pct: Optional[float]
    trailing_pct: Optional[float]
    allowed_tokens: list[str]
    max_drawdown_pct: Optional[float]
    min_trades_per_day: Optional[int]


@dataclass
class Config:
    # --- сеть (общее) ---
    rpc_urls: list[str]
    chain_id: int
    router: str
    wbnb: str
    usdt: str
    # --- кошельки / twak ---
    wallets: list[Wallet]
    twak_bin: str
    twak_password_env: str
    # --- газ (общее; gas_multiplier/floor — инфра) ---
    gas_price_gwei: Optional[float]
    gas_multiplier: float
    gas_floor_gwei: float
    gas_limit_fallback: int
    # --- исполнение (общее) ---
    deadline_secs: int
    receipt_timeout_secs: int
    approve_mode: str
    poll_secs: int
    aggregator: Optional[str]
    state_db: str
    # --- ГЛОБАЛЬНЫЕ ДЕФОЛТЫ стратегии (наследуются телом, если не переопределит) ---
    default_slippage_pct: float
    slippage_by_token: dict[str, float]
    bnb_gas_reserve: float
    take_profit_pct: Optional[float]
    stop_loss_pct: Optional[float]
    trailing_pct: Optional[float]
    allowed_tokens: list[str]
    max_drawdown_pct: Optional[float]
    min_trades_per_day: Optional[int]
    # верхний потолок проскальзывания (защита от ордера с slippage_pct=0.9 и т.п.) (#7)
    max_slippage_pct: float = 0.15
    # короткий таймаут подтверждения в live-цикле: своп уходит, ждём недолго, дальше
    # резолвим по receipt на следующих тиках — чтобы НЕ блокировать поллинг др. тел (#8)
    loop_confirm_secs: int = 12
    # Telegram-алерты: все WARNING/ERROR (тихие отказы, nonce-дыры, мёртвые стопы) + старт/heartbeat
    tg_bot_token: Optional[str] = None
    tg_chat_id: Optional[str] = None
    # PancakeSwap V3 (BSC, канонические): котировка через QuoterV2, своп через V3 SwapRouter.
    v3_router: str = "0x1b81D678ffb9C0263b24A97847620C99d213eB14"
    v3_quoter: str = "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"
    enable_v3: bool = True

    def params_for(self, wallet_name: str) -> WalletParams:
        """Слить глобальные дефолты с оверрайдами тела."""
        w = next((x for x in self.wallets if x.name == wallet_name), None)
        ov = (w.strategy if w else {}) or {}

        def pick(key, default):
            return ov[key] if key in ov and ov[key] is not None else default

        # ВСЕГДА отдаём КОПИЮ list/dict: иначе тело без оверрайда делит общий объект
        # с глобалом и другими телами по ссылке → мутация течёт между телами (#3).
        sl_by_tok = ov.get("slippage_by_token")
        sl_by_tok = ({k.lower(): v for k, v in sl_by_tok.items()} if sl_by_tok
                     else dict(self.slippage_by_token))
        allowed = ov.get("allowed_tokens")
        allowed = ([t.lower() for t in allowed] if allowed is not None
                   else list(self.allowed_tokens))
        return WalletParams(
            default_slippage_pct=pick("default_slippage_pct", self.default_slippage_pct),
            slippage_by_token=sl_by_tok,
            bnb_gas_reserve=pick("bnb_gas_reserve", self.bnb_gas_reserve),
            take_profit_pct=pick("take_profit_pct", self.take_profit_pct),
            stop_loss_pct=pick("stop_loss_pct", self.stop_loss_pct),
            trailing_pct=pick("trailing_pct", self.trailing_pct),
            allowed_tokens=allowed,
            max_drawdown_pct=pick("max_drawdown_pct", self.max_drawdown_pct),
            min_trades_per_day=pick("min_trades_per_day", self.min_trades_per_day),
        )


def _resolve_secret(v: str) -> str:
    """Поддержка 'env:NAME' и 'keystore:PATH' — приватку не держим в файле открыто."""
    if not isinstance(v, str):
        return v
    if v.startswith("env:"):
        name = v[4:]
        val = os.environ.get(name)
        if not val:
            raise RuntimeError(f"env var {name} не задана")
        return val
    if v.startswith("keystore:"):
        from eth_account import Account
        path = v[len("keystore:"):]
        pw = os.environ.get("TWAK_KEYSTORE_PASSWORD")
        if not pw:
            raise RuntimeError("TWAK_KEYSTORE_PASSWORD не задана для keystore")
        with open(path) as f:
            import json
            k = Account.decrypt(json.load(f), pw).hex()
            return k if k.startswith("0x") else "0x" + k   # M3: на eth_account .hex() без 0x
    return v


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    wallets = [
        Wallet(
            name=w["name"],
            address=w["address"],
            private_key=_resolve_secret(w["private_key"]),
            strategy=w.get("strategy") or {},
        )
        for w in raw["wallets"]
    ]

    rpc_urls = raw.get("rpc_urls") or ([raw["rpc_url"]] if raw.get("rpc_url") else [])
    if not rpc_urls:
        raise RuntimeError("нужен rpc_urls (список) или rpc_url в конфиге")

    # #10: address в конфиге ДОЛЖЕН совпадать с адресом из private_key — иначе свопы
    # уйдут на адрес ключа, а breaker/reconcile будут следить за чужим (drawdown ~0 →
    # защита от дисквала молча выключена).
    from eth_account import Account
    for w in wallets:
        try:
            derived = Account.from_key(w.private_key).address.lower()
        except Exception as e:
            raise RuntimeError(f"тело {w.name}: невалидный private_key ({e})")
        if derived != w.address.lower():
            raise RuntimeError(
                f"тело {w.name}: address {w.address} НЕ соответствует private_key "
                f"(ключ даёт {Account.from_key(w.private_key).address})")

    cfg = Config(
        rpc_urls=rpc_urls,
        chain_id=raw.get("chain_id", 56),
        router=raw.get("router", "0x10ED43C718714eb63d5aA57B78B54704E256024E"),
        wbnb=raw.get("wbnb", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"),
        usdt=raw.get("usdt", "0x55d398326f99059fF775485246999027B3197955"),
        wallets=wallets,
        twak_bin=raw.get("twak_bin", "twak"),
        twak_password_env=raw.get("twak_password_env", "TWAK_WALLET_PASSWORD"),
        gas_price_gwei=raw.get("gas_price_gwei"),
        gas_multiplier=raw.get("gas_multiplier", 1.15),
        gas_floor_gwei=raw.get("gas_floor_gwei", 0.05),
        gas_limit_fallback=raw.get("gas_limit_fallback", 350_000),
        deadline_secs=raw.get("deadline_secs", 120),
        receipt_timeout_secs=raw.get("receipt_timeout_secs", 180),
        approve_mode=raw.get("approve_mode", "infinite"),
        poll_secs=raw.get("poll_secs", 15),
        aggregator=raw.get("aggregator"),
        state_db=raw.get("state_db", "state.db"),
        # глобальные дефолты стратегии
        default_slippage_pct=raw.get("default_slippage_pct", 0.01),
        slippage_by_token={k.lower(): v for k, v in (raw.get("slippage_by_token") or {}).items()},
        bnb_gas_reserve=raw.get("bnb_gas_reserve", 0.003),
        take_profit_pct=raw.get("take_profit_pct"),
        stop_loss_pct=raw.get("stop_loss_pct"),
        trailing_pct=raw.get("trailing_pct"),
        allowed_tokens=[t.lower() for t in (raw.get("allowed_tokens") or [])],
        max_drawdown_pct=raw.get("max_drawdown_pct"),
        min_trades_per_day=raw.get("min_trades_per_day"),
        max_slippage_pct=raw.get("max_slippage_pct", 0.15),
        loop_confirm_secs=raw.get("loop_confirm_secs", 12),
        tg_bot_token=raw.get("tg_bot_token"),
        tg_chat_id=str(raw["tg_chat_id"]) if raw.get("tg_chat_id") is not None else None,
        v3_router=raw.get("v3_router", "0x1b81D678ffb9C0263b24A97847620C99d213eB14"),
        v3_quoter=raw.get("v3_quoter", "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"),
        enable_v3=raw.get("enable_v3", True),
    )

    # M3: пустой вайтлист = fail-open (любой токен пройдёт) — на конкурсе риск дисквала.
    for w in wallets:
        if not cfg.params_for(w.name).allowed_tokens:
            log.warning("тело %s: allowed_tokens ПУСТ — вайтлист НЕ ограничивает (fail-open!)", w.name)

    # Валидация конфига (fail-fast вместо тихого кривого поведения в бою):
    names = [w.name for w in wallets]
    if len(names) != len(set(names)):
        raise RuntimeError(f"дубли имён тел в конфиге: {[n for n in names if names.count(n) > 1]}")
    for w in wallets:
        p = cfg.params_for(w.name)
        if not (0 < p.default_slippage_pct < 1):
            raise RuntimeError(f"тело {w.name}: default_slippage_pct={p.default_slippage_pct} вне (0,1)")
        if p.max_drawdown_pct is not None and p.max_drawdown_pct <= 0:
            raise RuntimeError(f"тело {w.name}: max_drawdown_pct={p.max_drawdown_pct} <=0 = вечная заморозка BUY")
        if p.min_trades_per_day is not None and p.min_trades_per_day < 0:
            raise RuntimeError(f"тело {w.name}: min_trades_per_day={p.min_trades_per_day} отрицательный")
    return cfg


def slippage_for(params: WalletParams, token: str, order_slippage: Optional[float],
                 max_slippage: float = 0.15) -> float:
    """Приоритет: явный в ордере → пер-токен тела → дефолт тела. Клэмп сверху (#7)."""
    if order_slippage is not None:
        slip = order_slippage
    else:
        slip = params.slippage_by_token.get(token.lower(), params.default_slippage_pct)
    if slip > max_slippage:
        log.warning("slippage %.3f для %s обрезан до потолка %.3f", slip, token, max_slippage)
        slip = max_slippage
    return slip


def is_token_allowed(params: WalletParams, token: str) -> bool:
    """Вайтлист тела (пер-тело). Пустой список = ограничения нет."""
    if not params.allowed_tokens:
        return True
    return token.lower() in params.allowed_tokens
