#!/usr/bin/env python3
"""Entra ID provisioning tool — automates new-hire setup and offboarding.

Phase 1: authenticate with the client-credentials flow and list tenant
users, proving auth works before anything that writes.
"""

import argparse
import sys

from graph_api import ConfigError, GraphClient, GraphError


def cmd_list_users(args):
    """Print every user in the tenant — the auth smoke test."""
    client = GraphClient.from_env()
    users = client.list_users()
    if not users:
        print("No users found in the tenant.")
        return

    print(f"{len(users)} user(s) in tenant:\n")
    width = max(len(user.get("displayName") or "(no name)") for user in users)
    for user in users:
        name = user.get("displayName") or "(no name)"
        upn = user.get("userPrincipalName") or "?"
        status = "enabled" if user.get("accountEnabled") else "disabled"
        title = user.get("jobTitle")
        line = f"  {name:<{width}}  {upn}  [{status}]"
        if title:
            line += f"  {title}"
        print(line)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="provision",
        description="Automate Entra ID account provisioning via Microsoft Graph.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_users = subparsers.add_parser(
        "list-users", help="list all users in the tenant (auth smoke test)"
    )
    list_users.set_defaults(func=cmd_list_users)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, GraphError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
