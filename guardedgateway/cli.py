"""Admin CLI: create-tenant, create-key, set-cap."""

from __future__ import annotations

import argparse
import sys

from guardedgateway.tenants import get_tenant_store


def cmd_create_tenant(args: argparse.Namespace) -> int:
    store = get_tenant_store()
    store.create_tenant(args.name)
    print(f"tenant created: {args.name}")
    return 0


def cmd_create_key(args: argparse.Namespace) -> int:
    store = get_tenant_store()
    api_key = store.create_key(
        args.tenant,
        cap_usd=args.cap,
        phi=args.phi,
        inject_guard=args.inject_guard,
    )
    print(api_key)
    return 0


def cmd_set_cap(args: argparse.Namespace) -> int:
    store = get_tenant_store()
    ok = store.set_cap(args.api_key, args.cap)
    if not ok:
        print(f"error: no such api key {args.api_key!r}", file=sys.stderr)
        return 1
    print(f"cap set: {args.api_key} -> ${args.cap:.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guardedgateway")
    sub = parser.add_subparsers(dest="command", required=True)

    p_tenant = sub.add_parser("create-tenant", help="create a tenant")
    p_tenant.add_argument("name")
    p_tenant.set_defaults(func=cmd_create_tenant)

    p_key = sub.add_parser("create-key", help="create an api key for a tenant")
    p_key.add_argument("tenant")
    p_key.add_argument("--cap", type=float, default=None, help="monthly cap in USD")
    p_key.add_argument("--phi", action="store_true", help="treat all traffic on this key as PHI")
    p_key.add_argument(
        "--inject-guard", action="store_true", help="enable prompt-injection redaction"
    )
    p_key.set_defaults(func=cmd_create_key)

    p_cap = sub.add_parser("set-cap", help="update an existing key's cap")
    p_cap.add_argument("api_key")
    p_cap.add_argument("cap", type=float)
    p_cap.set_defaults(func=cmd_set_cap)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
