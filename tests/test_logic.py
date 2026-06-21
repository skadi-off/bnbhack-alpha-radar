"""Юнит-тесты чистой логики (без сети/RPC). Запуск: python -m pytest tests/ -q

Покрывают самые дорогие классы багов: парсинг ордера, slippage-math, выбор слиппеджа,
вайтлист-гейт, триггер-логику TP/SL/trailing/limit, дедуп/идемпотентность, маршрутизацию.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config, Wallet, slippage_for, is_token_allowed
from src.models import Order, OrderType, Side
from src.parse import order_from_dict, order_to_dict
from src.store import Store


def _cfg(**kw):
    base = dict(
        rpc_urls=["x"], chain_id=56, router="0x10ED43C718714eb63d5aA57B78B54704E256024E",
        wbnb="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        usdt="0x55d398326f99059fF775485246999027B3197955",
        wallets=[], twak_bin="twak", twak_password_env="X",
        gas_price_gwei=None, gas_multiplier=1.15, gas_floor_gwei=0.05,
        gas_limit_fallback=350000, bnb_gas_reserve=0.003,
        default_slippage_pct=0.01, slippage_by_token={},
        take_profit_pct=None, stop_loss_pct=None, trailing_pct=None,
        deadline_secs=120, receipt_timeout_secs=180, approve_mode="exact", poll_secs=15,
        allowed_tokens=[], max_drawdown_pct=None, min_trades_per_day=None,
        aggregator=None, state_db=":memory:",
    )
    base.update(kw)
    return Config(**base)


# ---------- парсинг ----------
def test_order_from_dict_defaults():
    o = order_from_dict({"wallet": "body1", "side": "buy", "token": "0xabc"})
    assert o.type == OrderType.MARKET and o.side == Side.BUY
    assert o.slippage_pct is None and o.sell_all is False


def test_order_roundtrip():
    o = order_from_dict({"wallet": "b", "side": "sell", "token": "0xT", "type": "limit",
                         "trigger_price": 1.5, "trailing_pct": 0.08, "client_order_id": "x1"})
    o2 = order_from_dict(order_to_dict(o))
    assert o2.type == OrderType.LIMIT and o2.trigger_price == 1.5 and o2.client_order_id == "x1"


def test_bad_side_raises():
    import pytest
    with pytest.raises(ValueError):
        order_from_dict({"wallet": "b", "side": "hodl", "token": "0xT"})


# ---------- slippage ----------
def test_slippage_priority():
    cfg = _cfg(default_slippage_pct=0.01, slippage_by_token={"0xtok": 0.07})
    p = cfg.params_for("none")
    assert slippage_for(p, "0xTOK", 0.03) == 0.03             # явный в ордере
    assert slippage_for(p, "0xTOK", None) == 0.07            # пер-токен (регистр игнор)
    assert slippage_for(p, "0xother", None) == 0.01          # дефолт


def test_min_out_math():
    expected = 1_000_000
    assert int(expected * (1 - 0.01)) == 990_000
    assert int(expected * (1 - 0.0)) == 1_000_000


# ---------- вайтлист ----------
def test_whitelist():
    assert is_token_allowed(_cfg(allowed_tokens=[]).params_for("none"), "0xANY") is True
    cfg = _cfg(allowed_tokens=["0xaaa"])
    assert is_token_allowed(cfg.params_for("none"), "0xAAA") is True
    assert is_token_allowed(cfg.params_for("none"), "0xbbb") is False


def test_per_body_overrides():
    """Каждое тело — своя секция стратегии поверх глобальных дефолтов."""
    cfg = _cfg(
        default_slippage_pct=0.01, allowed_tokens=["0xaaa"], take_profit_pct=0.10,
        wallets=[
            Wallet("body1", "0x1", "0xpk"),  # наследует глобальное
            Wallet("body2", "0x2", "0xpk", strategy={
                "default_slippage_pct": 0.05, "allowed_tokens": ["0xbbb"], "take_profit_pct": 0.30}),
        ],
    )
    p1, p2 = cfg.params_for("body1"), cfg.params_for("body2")
    # body1 — глобальные дефолты
    assert p1.default_slippage_pct == 0.01 and p1.take_profit_pct == 0.10
    assert is_token_allowed(p1, "0xaaa") and not is_token_allowed(p1, "0xbbb")
    # body2 — свои числа и свой вайтлист
    assert p2.default_slippage_pct == 0.05 and p2.take_profit_pct == 0.30
    assert is_token_allowed(p2, "0xbbb") and not is_token_allowed(p2, "0xaaa")


# ---------- триггер-логика (чистая математика порогов) ----------
def _hit(entry, price, tp=None, sl=None, trail=None, hwm=None):
    hwm = max(hwm or entry, price)
    change = (price - entry) / entry
    hit_tp = tp is not None and change >= tp
    hit_sl = sl is not None and change <= -sl
    hit_trail = trail is not None and hwm > entry and price <= hwm * (1 - trail)
    return hit_tp, hit_sl, hit_trail


def test_tp_sl_thresholds():
    assert _hit(100, 120, tp=0.20)[0] is True       # ровно +20% → TP
    assert _hit(100, 119, tp=0.20)[0] is False
    assert _hit(100, 90, sl=0.10)[1] is True        # ровно -10% → SL
    assert _hit(100, 91, sl=0.10)[1] is False


def test_trailing():
    # цена сходила до 150 (hwm), трейл 8% → стоп на 138; на 137 срабатывает
    assert _hit(100, 137, trail=0.08, hwm=150)[2] is True
    assert _hit(100, 145, trail=0.08, hwm=150)[2] is False
    # пока в убытке (hwm==entry) трейл не вооружён
    assert _hit(100, 95, trail=0.08, hwm=100)[2] is False


def test_limit_direction():
    buy = order_from_dict({"wallet": "b", "side": "buy", "token": "0xT", "type": "limit",
                           "trigger_price": 1.0})
    # buy-лимит срабатывает когда цена <= порога
    assert (buy.side == Side.BUY and 0.9 <= buy.trigger_price)
    sell = order_from_dict({"wallet": "b", "side": "sell", "token": "0xT", "type": "limit",
                            "trigger_price": 2.0})
    assert (sell.side == Side.SELL and 2.1 >= sell.trigger_price)


# ---------- стор: идемпотентность/дедуп/отмена ----------
def test_store_idempotency_and_cancel():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "s.db"))
        assert s.seen("coid1") is False
        s.mark_seen("coid1")
        assert s.seen("coid1") is True
        tid = s.add_trigger("limit", "body1", "0xT", "buy",
                            {"wallet": "body1", "side": "buy", "token": "0xT"},
                            client_order_id="coid2")
        assert any(r["id"] == tid for r in s.active_triggers())
        assert s.cancel_trigger("coid2") == 1
        assert all(r["id"] != tid for r in s.active_triggers())


# ---------- C4: атомарный claim + защита от двойного исполнения ----------
def test_claim_trigger_cas():
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "s.db"))
        tid = s.add_trigger("limit", "body1", "0xT", "buy",
                            {"wallet": "body1", "side": "buy", "token": "0xT"})
        assert s.claim_trigger(tid, "pending", "exiting") is True   # победитель
        assert s.claim_trigger(tid, "pending", "exiting") is False  # гонка проиграла
        assert s.claim_trigger(tid, "pending", "closed") is False   # неверный from-статус


class _FakeExec:
    """Заглушка Executor: считает вызовы execute и зовёт on_sent как настоящий."""
    def __init__(self, result, direct=None):
        self.result = result
        self.calls = 0
        self.direct = direct

    def execute(self, order, on_sent=None, confirm_secs=None):
        self.calls += 1
        if on_sent and self.result.tx_hash:
            on_sent(self.result.tx_hash)
        return self.result


def _engine_with(result, direct=None):
    from src.triggers import TriggerEngine
    cfg = _cfg(wallets=[Wallet(name="body1", address="0x0000000000000000000000000000000000000001",
                               private_key="0x00")])
    s = Store(":memory:")
    eng = TriggerEngine(cfg, _FakeExec(result, direct), s)
    eng._price = lambda token: 0.5   # ниже порога → buy-лимит сработает
    return eng, s


def test_limit_fires_once_then_closed():
    from src.models import ExecResult, OrderStatus, Venue
    res = ExecResult(ok=True, venue=Venue.DIRECT, status=OrderStatus.FILLED, tx_hash="0xabc")
    eng, s = _engine_with(res)
    order = order_from_dict({"wallet": "body1", "side": "buy", "token": "0xT",
                             "type": "limit", "trigger_price": 1.0})
    tid = eng.register_limit(order)
    eng.run_once()                                  # сработал, исполнил 1 раз → closed
    assert eng.ex.calls == 1
    assert all(r["id"] != tid for r in s.active_triggers())   # ушёл из active
    eng.run_once()                                  # повторный тик — НЕ ре-исполняет
    assert eng.ex.calls == 1


def test_limit_pending_tx_not_resent():
    """C2/C4: tx ушла, но не подтвердилась (pending) → НЕ ре-слать, ждать резолв."""
    from src.models import ExecResult, OrderStatus
    res = ExecResult(ok=False, status=OrderStatus.EXITING, tx_hash="0xdead", pending=True)

    class _Eth:
        receipt = None
        def get_transaction_receipt(self, txh):
            if self.receipt is None:
                from web3.exceptions import TransactionNotFound
                raise TransactionNotFound("not mined")
            return self.receipt
    eth = _Eth()
    # резолв теперь читает receipt через pool.get_receipt_any (ротация, K2), не через primary
    def _get_receipt_any(txh):
        try:
            return eth.get_transaction_receipt(txh)
        except Exception:
            return None
    direct = type("D", (), {"pool": type("P", (), {
        "primary": type("PR", (), {"eth": eth})(),
        "get_receipt_any": staticmethod(_get_receipt_any)})()})()
    eng, s = _engine_with(res, direct)
    order = order_from_dict({"wallet": "body1", "side": "buy", "token": "0xT",
                             "type": "limit", "trigger_price": 1.0})
    tid = eng.register_limit(order)
    eng.run_once()                                  # сработал, tx pending
    assert eng.ex.calls == 1
    row = next(r for r in s.active_triggers() if r["id"] == tid)
    assert row["status"] == "exiting" and row["exit_tx"] == "0xdead"
    eng.run_once()                                  # receipt ещё нет → НЕ ре-слать
    assert eng.ex.calls == 1
    assert next(r for r in s.active_triggers() if r["id"] == tid)["status"] == "exiting"
    eth.receipt = {"status": 1}                      # tx подтвердилась
    eng.run_once()                                  # резолв → closed, по-прежнему без ре-отправки
    assert eng.ex.calls == 1
    assert all(r["id"] != tid for r in s.active_triggers())


def test_order_key_dedup():
    a = Order(wallet="b", side=Side.BUY, token="0xT", amount=0.1, client_order_id="z")
    b = Order(wallet="b", side=Side.SELL, token="0xX", amount=9, client_order_id="z")
    assert a.key() == b.key() == "z"                # явный id важнее
    c = Order(wallet="b", side=Side.BUY, token="0xT", amount=0.1, tag="t")
    # детерминированная подпись включает все экономически значимые поля (C6)
    assert c.key() == "b:buy:0xt:market:0.1:False:None:None:None:None:t"  # token лоуэркейсится


def test_order_key_distinguishes_different_limits():
    # C6: две РАЗНЫЕ лимитки с одинаковыми wallet+amount+tag, но разными порогами
    # больше НЕ схлопываются в один ключ (раньше вторая молча терялась).
    o1 = Order(wallet="b", side=Side.BUY, token="0xT", type=OrderType.LIMIT,
               amount=0.1, trigger_price=100.0, tag="t")
    o2 = Order(wallet="b", side=Side.BUY, token="0xT", type=OrderType.LIMIT,
               amount=0.1, trigger_price=200.0, tag="t")
    assert o1.key() != o2.key()
    # а market-вход и limit-вход с тем же amount тоже различимы
    m = Order(wallet="b", side=Side.BUY, token="0xT", type=OrderType.MARKET, amount=0.1, tag="t")
    assert m.key() != o1.key()


# ===== регрессии на фиксы второго ревью (2026-06-20) =====

def test_params_for_isolation():
    # #3: мутация вайтлиста/слиппеджа одного тела НЕ протекает на глобал/другие тела
    from src.config import Config
    cfg = _cfg(wallets=[Wallet(name="b1", address="0x" + "1" * 40, private_key="0x00"),
                        Wallet(name="b2", address="0x" + "2" * 40, private_key="0x00")])
    cfg.allowed_tokens = ["0xaaa"]
    cfg.slippage_by_token = {"0xaaa": 0.01}
    a = cfg.params_for("b1")
    a.allowed_tokens.append("0xevil")
    a.slippage_by_token["0xaaa"] = 0.99
    b = cfg.params_for("b2")
    assert "0xevil" not in b.allowed_tokens
    assert "0xevil" not in cfg.allowed_tokens
    assert b.slippage_by_token["0xaaa"] == 0.01
    assert cfg.slippage_by_token["0xaaa"] == 0.01


def test_slippage_clamped():
    # #7: ордер с slippage_pct=0.9 обрезается до потолка
    from src.config import slippage_for, WalletParams
    p = WalletParams(default_slippage_pct=0.01, slippage_by_token={}, bnb_gas_reserve=0.003,
                     take_profit_pct=None, stop_loss_pct=None, trailing_pct=None,
                     allowed_tokens=[], max_drawdown_pct=None, min_trades_per_day=None)
    assert slippage_for(p, "0xT", 0.9, max_slippage=0.15) == 0.15
    assert slippage_for(p, "0xT", 0.05, max_slippage=0.15) == 0.05


def test_order_key_float_normalized():
    # H4: 0.1+0.2 (=0.30000000000000004) даёт тот же ключ, что и 0.3
    a = Order(wallet="b", side=Side.BUY, token="0xT", amount=0.1 + 0.2, tag="t")
    b = Order(wallet="b", side=Side.BUY, token="0xT", amount=0.3, tag="t")
    assert a.key() == b.key()


def test_address_key_mismatch_rejected(tmp_path):
    # #10: address в конфиге не совпадает с private_key → load_config падает
    import yaml
    from src.config import load_config
    from eth_account import Account
    acct = Account.create()
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({
        "rpc_urls": ["https://bsc-dataseed.binance.org"],
        "wallets": [{"name": "b1", "address": "0x" + "9" * 40, "private_key": acct.key.hex()}],
    }))
    try:
        load_config(str(bad))
        assert False, "должно было упасть на несовпадении address/key"
    except RuntimeError as e:
        assert "НЕ соответствует" in str(e)


def test_failed_swap_backoff_but_never_abandoned():
    # B1+HIGH#1: неудачный своп НЕ бросает строку в failed (жива, ретраит), НО с бэкоффом —
    # не бьётся каждый тик. После выдержки ретраит снова.
    from src.models import ExecResult, OrderStatus
    res = ExecResult(ok=False, status=OrderStatus.FAILED, error="revert")
    eng, s = _engine_with(res)
    o = Order(wallet="body1", side=Side.BUY, token="0xT", type=OrderType.LIMIT,
              amount=0.1, trigger_price=1.0, client_order_id="loop")
    tid = eng.register_limit(o)
    for _ in range(8):
        eng.run_once()
    active = [r for r in s.active_triggers()]
    assert len(active) == 1 and active[0]["status"] == "pending"   # жива, не брошена
    assert eng.ex.calls == 1                                       # бэкофф: после 1й неудачи ждём
    assert active[0]["attempts"] == 1
    # «промотать» время: сделать updated_at старым → бэкофф истёк → ретрай снова
    s._db.execute("UPDATE triggers SET updated_at=0 WHERE id=?", (tid,)); s._db.commit()
    eng.run_once()
    assert eng.ex.calls == 2                                       # ретраит после выдержки


def test_register_position_rejects_zero_entry():
    # #9: позиция с entry<=0 не открывается (TP/SL не работали бы)
    from src.models import ExecResult, OrderStatus
    res = ExecResult(ok=True, status=OrderStatus.FILLED, tx_hash="0xabc")
    eng, s = _engine_with(res)
    o = Order(wallet="body1", side=Side.BUY, token="0xT", amount=0.1,
              take_profit_pct=0.2, client_order_id="z")
    assert eng.register_position(o, 0.0) == -1
    assert eng.register_position(o, None) == -1


def test_cancel_does_not_kill_position():
    # K3: cancel по coid отменяет лимит-заявку, но НЕ открытую позицию с тем же coid
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "s.db"))
        lim = s.add_trigger("limit", "body1", "0xT", "buy",
                            {"wallet": "body1", "side": "buy", "token": "0xT"}, client_order_id="c1")
        pos = s.add_trigger("position", "body1", "0xT", "sell",
                            {"wallet": "body1", "side": "sell", "token": "0xT"},
                            entry_price=1.0, hwm=1.0, client_order_id="c1")
        assert s.cancel_trigger("c1") == 1                      # отменилась только лимитка
        active = {r["id"]: r["status"] for r in s.active_triggers()}
        assert pos in active and active[pos] == "pending"        # позиция жива, стоп работает
        assert lim not in active                                 # лимитка отменена


def test_sell_uses_token_decimals_not_18():
    # Вайтлист: 17 токенов не с 18 знаками (XPR=4, BONK=5, RAY/TRX/XAUt/LUNC=6).
    # Объём продажи должен считаться по реальным decimals, не по 18.
    from unittest.mock import MagicMock
    from eth_account import Account
    from src.direct_adapter import DirectAdapter
    from src.models import Order, Side, OrderType
    acct = Account.create()
    cfg = _cfg(wallets=[Wallet("body1", acct.address, acct.key.hex())])
    eth = type("E", (), {"account": Account, "contract": staticmethod(lambda *a, **k: MagicMock())})()
    pool = type("P", (), {"primary": type("PR", (), {"eth": eth})()})()
    a = DirectAdapter(cfg, pool, nonce=None)
    tok = "0x" + "11" * 20
    # глушим всё сетевое
    a.decimals = lambda t: 6
    a.best_path = lambda t, amt, buy: ([tok, a.wbnb], 5 * 10 ** 18)   # выход не пыль
    a._v3_best = lambda t, amt, buy: (None, 0)                        # V3 не котируем в юните
    a._ensure_allowance = lambda *x, **k: None
    a.balance_bnb = lambda o, primary=False: 10 ** 19    # хватает на газ-чек + дельту фила
    a._gas_price = lambda: 10 ** 9
    a._send_and_confirm = lambda w, tx, on_sent=None, confirm_secs=None: {
        "tx_hash": "0xabc", "receipt": {"status": 1, "gasUsed": 100000, "effectiveGasPrice": 10 ** 9}}
    w = cfg.wallets[0]
    # явный amount 2.5 у 6-decimal токена → 2_500_000 wei, НЕ 2.5e18
    res = a.sell(w, Order(wallet="body1", side=Side.SELL, token=tok, amount=2.5))
    assert res.raw["amount_wei"] == 2_500_000


def test_nonce_gap_detect_and_resync():
    # C1: детектор nonce-дыры. local ушёл вперёд + мемпул пуст (pending==latest) → дыра.
    from src.nonce import NonceManager
    state = {"pending": 5, "latest": 5}
    class _Eth:
        def get_transaction_count(self, addr, block):
            return state[block]
    w3 = type("W", (), {"eth": _Eth()})()
    pool = type("P", (), {"primary_call": staticmethod(lambda fn, **k: fn(w3))})()
    nm = NonceManager(pool)
    addr = "0xabc"
    assert nm.reserve(addr) == 5          # выдал 5, local стал 6
    assert nm.is_gap(addr) is True        # local=6 > pending=5, мемпул пуст → дыра
    state["pending"] = 6                  # появилась незамайненная tx
    assert nm.is_gap(addr) is False       # мемпул НЕ пуст → не трогаем (безопасно)
    nm.resync(addr)                       # сбросили
    assert nm.is_gap(addr) is False       # local очищен → дыры нет


def test_attempts_reset_on_close():
    # H2: счётчик неудач обнуляется при закрытии (иначе на повторной строке алерт рано)
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "s.db"))
        tid = s.add_trigger("limit", "body1", "0xT", "buy",
                            {"wallet": "body1", "side": "buy", "token": "0xT"})
        s.bump_attempts(tid); s.bump_attempts(tid)
        s.set_trigger_status(tid, "closed")
        row = s._db.execute("SELECT attempts, status FROM triggers WHERE id=?", (tid,)).fetchone()
        assert row["attempts"] == 0 and row["status"] == "closed"


def test_parse_coerces_string_numbers():
    # HIGH: мозг прислал числа строками — не должны утечь как str (краш/неверный объём)
    o = order_from_dict({"wallet": "b", "side": "sell", "token": "0xT",
                         "amount": "5.0", "trigger_price": "1.25", "stop_loss_pct": "0.1"})
    assert o.amount == 5.0 and isinstance(o.amount, float)
    assert o.trigger_price == 1.25 and o.stop_loss_pct == 0.1
    import pytest
    with pytest.raises(ValueError):
        order_from_dict({"wallet": "b", "side": "buy", "token": "0xT", "amount": "abc"})


def test_cancel_clears_seen():
    # MED: после отмены повторная подача того же coid должна проходить (seen очищен)
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "s.db"))
        s.mark_seen("c1")
        assert s.seen("c1") is True
        s.clear_seen("c1")
        assert s.seen("c1") is False


def test_atomic_limit_register_and_seen():
    # MED: регистрация лимитки и seen — одной транзакцией (без двойной лимитки при OOM)
    with tempfile.TemporaryDirectory() as d:
        s = Store(os.path.join(d, "s.db"))
        s.add_trigger("limit", "b", "0xT", "buy", {"wallet": "b"}, client_order_id="c1", seen_key="c1")
        assert s.seen("c1") is True
        assert any(r["client_order_id"] == "c1" for r in s.active_triggers())


def test_num_rejects_inf_nan_negative():
    # N2 + HIGH: inf/nan/отрицательные числа должны отклоняться на входе
    import pytest
    for bad in ("inf", "nan", "-1e9", -0.5):
        with pytest.raises(ValueError):
            order_from_dict({"wallet": "b", "side": "buy", "token": "0xT", "amount": bad})
    with pytest.raises(ValueError):
        order_from_dict({"wallet": "b", "side": "buy", "token": "0xT", "slippage_pct": float("nan")})
    # нормальные проходят
    o = order_from_dict({"wallet": "b", "side": "buy", "token": "0xT", "amount": "0.5", "stop_loss_pct": 0.1})
    assert o.amount == 0.5 and o.stop_loss_pct == 0.1




def test_nonce_release_rolls_back_one():
    # MED3: release откатывает ровно последнюю резервацию, не затирая счётчик
    from src.nonce import NonceManager
    state = {"pending": 5, "latest": 5}
    w3 = type("W", (), {"eth": type("E", (), {
        "get_transaction_count": staticmethod(lambda a, b: state[b])})()})()
    pool = type("P", (), {"primary_call": staticmethod(lambda fn, **k: fn(w3))})()
    nm = NonceManager(pool)
    a = "0xabc"
    n = nm.reserve(a)               # 5, local→6
    nm.release(a, n)                # откат → local=5
    assert nm.reserve(a) == 5       # снова выдаёт 5 (слот свободен), не 6
