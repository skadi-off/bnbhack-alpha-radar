from __future__ import annotations

import argparse
import getpass
import json
import os

from eth_account import Account


def main() -> None:
    """Зашифровать приватку в scrypt-keystore (eth-account). Затем в config.yaml:
        private_key: "keystore:keystores/body1.json"
    и экспортнуть TWAK_KEYSTORE_PASSWORD перед запуском.

    Снижает blast-radius: ключ не лежит открытым в конфиге/env.
    """
    ap = argparse.ArgumentParser(description="Зашифровать приватку в keystore-файл")
    ap.add_argument("--out", required=True, help="путь к keystore .json")
    ap.add_argument("--pk-env", help="имя env с приваткой (иначе спросит интерактивно)")
    args = ap.parse_args()

    pk = os.environ.get(args.pk_env) if args.pk_env else getpass.getpass("private key: ")
    if not pk:
        raise SystemExit("приватка не задана")
    pw = os.environ.get("TWAK_KEYSTORE_PASSWORD") or getpass.getpass("keystore password: ")

    ks = Account.encrypt(pk, pw, kdf="scrypt")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(ks, f)
    os.chmod(args.out, 0o600)
    print(f"keystore сохранён: {args.out} (адрес 0x{ks['address']})")


if __name__ == "__main__":
    main()
