from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time

from web3 import Web3

from .config import load_config
from .direct_adapter import DirectAdapter
from .nonce import NonceManager
from .rpc import RpcPool

log = logging.getLogger("register")

# Контракт конкурса BNB Hack (Track 1), BSC — CompetitionRegistry (верифицирован).
COMPETITION_CONTRACT = "0x212c61b9b72c95d95bf29cf032f5e5635629aed5"

# Минимальный ABI: register() без аргументов + вью статуса/окна (фактчек).
REGISTRY_ABI = [
    {"name": "register", "type": "function", "stateMutability": "nonpayable", "inputs": [], "outputs": []},
    {"name": "isRegistered", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "bool"}]},
    {"name": "registrationStart", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "registrationDeadline", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]


def register_via_twak(twak_bin: str, wallet_name: str) -> str:
    # Реальная команда (фактчек): без --wallet, один twak-инстанс = один кошелёк.
    env = dict(os.environ)
    env["HOME"] = os.path.join(os.path.expanduser("~"), f".twak-{wallet_name}")
    proc = subprocess.run([twak_bin, "compete", "register", "--json"],
                          capture_output=True, text=True, timeout=120, env=env)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "ненулевой код")
    return proc.stdout.strip()


def register_direct(adapter: DirectAdapter, wallet, contract_addr: str) -> str:
    """Прямой register-tx как фоллбэк к twak. ABI.register() энкодит селектор сам."""
    addr = Web3.to_checksum_address(contract_addr)
    c = adapter.pool.primary.eth.contract(address=addr, abi=REGISTRY_ABI)
    # B4: окно читаем СТРОГО через primary — отстающая реплика вернёт 0/stale → ложное
    # «окно закрыто» → откажемся регать все 4 тела (вылет с конкурса). primary_call не ротирует.
    start = adapter.pool.primary_call(lambda w3: w3.eth.contract(address=addr, abi=REGISTRY_ABI)
                                      .functions.registrationStart().call())
    deadline = adapter.pool.primary_call(lambda w3: w3.eth.contract(address=addr, abi=REGISTRY_ABI)
                                         .functions.registrationDeadline().call())
    now = int(time.time())
    if now < start:
        raise RuntimeError(f"окно ещё не открыто (start={start}, now={now})")
    if now > deadline:
        raise RuntimeError(f"окно закрыто (deadline={deadline}, now={now})")
    acct = adapter.pool.primary.eth.account.from_key(wallet.private_key)
    tx = c.functions.register().build_transaction({"from": acct.address})
    tx.pop("nonce", None)
    tx.pop("gas", None)
    sent = adapter._send_and_confirm(wallet, tx)
    return sent["tx_hash"]


def is_registered(adapter: DirectAdapter, contract_addr: str, address: str) -> bool:
    addr = Web3.to_checksum_address(contract_addr)
    a = Web3.to_checksum_address(address)
    # B4: статус регистрации — через primary (отстающая реплика вернёт ложный False/True).
    return adapter.pool.primary_call(lambda w3: w3.eth.contract(address=addr, abi=REGISTRY_ABI)
                                     .functions.isRegistered(a).call())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Регистрация 4 тел на контракте конкурса")
    ap.add_argument("--config", default="config.yaml")
    # #4: ПО УМОЛЧАНИЮ direct — регаем ИМЕННО тот адрес, с которого торгуем (из private_key).
    # twak регистрировал бы свой адрес (наши ключи он не берёт) → тело не попало бы на конкурс.
    ap.add_argument("--use-twak", action="store_true", help="регать через twak (по умолч. direct)")
    ap.add_argument("--out", default="registration.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    pool = RpcPool(cfg.rpc_urls)
    adapter = DirectAdapter(cfg, pool, NonceManager(pool))

    log.info("контракт конкурса: %s", COMPETITION_CONTRACT)
    results = []
    for w in cfg.wallets:
        rec = {"wallet": w.name, "address": w.address}
        try:
            if is_registered(adapter, COMPETITION_CONTRACT, w.address):
                rec.update(ok=True, already=True)
                log.info("%s (%s) уже зареган", w.name, w.address)
                results.append(rec)
                continue
            if args.use_twak:
                try:
                    rec["twak_out"] = register_via_twak(cfg.twak_bin, w.name)
                except Exception as e:
                    log.warning("%s: twak не сработал (%s) → прямой register-tx", w.name, e)
                    rec["twak_error"] = str(e)
                    rec["tx_hash"] = register_direct(adapter, w, COMPETITION_CONTRACT)
            else:
                rec["tx_hash"] = register_direct(adapter, w, COMPETITION_CONTRACT)  # #4: direct по умолчанию
            # ончейн-подтверждение (не верить stdout)
            ok = is_registered(adapter, COMPETITION_CONTRACT, w.address)
            rec["ok"] = ok
            log.info("%s (%s): registered=%s tx=%s", w.name, w.address, ok, rec.get("tx_hash"))
        except Exception as e:
            rec.update(ok=False, error=str(e))
            log.error("%s НЕ зареган: %s", w.name, e)
        results.append(rec)

    with open(args.out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    ok_n = sum(1 for r in results if r.get("ok"))
    total = len(results)
    log.info("итог: %d/%d зарегано, детали в %s", ok_n, total, args.out)
    # B4: явный баннер + ненулевой код при частичной/полной неудаче — чтобы НЕ проглядеть,
    # что часть тел не попала на конкурс (one-shot, исправить можно только до дедлайна).
    if ok_n < total:
        bad = [r["wallet"] for r in results if not r.get("ok")]
        log.error("!!! НЕ ЗАРЕГАНЫ %d/%d: %s — ПЕРЕЗАПУСТИ register ДО дедлайна !!!",
                  total - ok_n, total, ", ".join(bad))
        import sys
        sys.exit(1)


if __name__ == "__main__":
    main()
