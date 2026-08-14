#!/usr/bin/env python3
"""Entra ID provisioning tool — automates new-hire setup and offboarding.

Commands:
  list-users            all users in the tenant (auth smoke test)
  discover 619          role accounts for a property number
  discover manager619   accounts matching a UPN prefix
  new                   create a fresh account for the hire in hire.yaml
  reuse                 hand an existing role account to the hire in hire.yaml
"""

import argparse
import secrets
import string
import sys
import unicodedata
from pathlib import Path

import yaml

from graph_api import ConfigError, GraphClient, GraphError

BASE_DIR = Path(__file__).parent
USER_FIELDS = "id,displayName,userPrincipalName,accountEnabled,jobTitle,officeLocation"


class ProvisionError(Exception):
    """A provisioning step can't proceed; the message says why."""


def load_yaml(name, hint):
    path = BASE_DIR / name
    if not path.exists():
        raise ProvisionError(f"{name} not found — {hint}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProvisionError(f"{name} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProvisionError(f"{name} must be a mapping of field: value lines")
    return data


def load_config():
    return load_yaml(
        "config.yaml", "copy config.example.yaml and fill in your tenant's values"
    )


def load_hire():
    hire = load_yaml(
        "hire.yaml", "create it with the fields from the hire form (see README)"
    )
    missing = [field for field in ("first_name", "last_name") if not hire.get(field)]
    if missing:
        raise ProvisionError(f"hire.yaml is missing {', '.join(missing)}")
    return hire


def temp_password():
    """Random temporary password meeting Entra's default complexity rules."""
    rng = secrets.SystemRandom()
    chars = (
        [rng.choice(string.ascii_uppercase) for _ in range(4)]
        + [rng.choice(string.ascii_lowercase) for _ in range(6)]
        + [rng.choice(string.digits) for _ in range(3)]
        + [rng.choice("!@#$%")]
    )
    rng.shuffle(chars)
    return "".join(chars)


def print_user(user, last_sign_in=None):
    name = user.get("displayName") or "(no name)"
    upn = user.get("userPrincipalName") or "?"
    status = "enabled" if user.get("accountEnabled") else "DISABLED"
    line = f"  {name:<24}  {upn:<48}  [{status}]"
    if user.get("jobTitle"):
        line += f"  {user['jobTitle']}"
    print(line)
    if last_sign_in:
        print(f"{'':28}last sign-in: {last_sign_in}")


def tenant_domain(config, required=True):
    domain = (config.get("tenant") or {}).get("domain")
    if not domain and required:
        raise ProvisionError("no tenant.domain in config.yaml")
    return domain


def cmd_list_users(args):
    client = GraphClient.from_env()
    users = client.list_users()
    if not users:
        print("No users found in the tenant.")
        return

    print(f"{len(users)} user(s) in tenant:\n")
    for user in users:
        print_user(user)


def cmd_discover(args):
    config = load_config()
    client = GraphClient.from_env()

    if args.target.isdigit():
        roles = (config.get("naming") or {}).get("roles") or []
        if not roles:
            raise ProvisionError(
                "no naming.roles in config.yaml — add the role-account prefixes "
                "to search by property number"
            )
        prefixes = [f"{role}{args.target}" for role in roles]
    else:
        prefixes = [args.target]

    # signInActivity needs AuditLog.Read.All plus Entra ID P1; degrade without it.
    sign_in_available = True
    found = False
    for prefix in prefixes:
        fields = USER_FIELDS + ",signInActivity" if sign_in_available else USER_FIELDS
        try:
            matches = client.find_users(prefix, fields)
        except GraphError as exc:
            if not sign_in_available or exc.status not in (400, 403):
                raise
            sign_in_available = False
            matches = client.find_users(prefix, USER_FIELDS)
        for user in matches:
            found = True
            last = (user.get("signInActivity") or {}).get("lastSignInDateTime")
            print_user(user, last)

    if not found:
        print(f"No accounts found for: {', '.join(prefixes)}")
        print("A fresh account is the likely path — run: python provision.py new")
    elif not sign_in_available:
        print("\n(last sign-in unavailable — needs AuditLog.Read.All and an Entra ID P1 license)")


def sanitize_local(text):
    """Reduce a name fragment to the ASCII letters/digits a UPN allows."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch for ch in ascii_text if ch.isalnum()).lower()


def build_upn(hire, config, override):
    domain = tenant_domain(config)
    if override:
        return override if "@" in override else f"{override}@{domain}"
    local = sanitize_local(hire["first_name"][:1] + hire["last_name"])
    if not local:
        raise ProvisionError("could not build a UPN from the hire's name — pass --upn")
    return f"{local}@{domain}"


def cmd_new(args):
    config = load_config()
    hire = load_hire()
    client = GraphClient.from_env()
    upn = build_upn(hire, config, args.upn)

    existing = client.get_user(upn, USER_FIELDS)
    if existing:
        print(f"{upn} already exists:")
        print_user(existing)
        local, domain = upn.split("@", 1)
        first2 = sanitize_local(hire["first_name"][:2])
        first_full = sanitize_local(hire["first_name"])
        last = sanitize_local(hire["last_name"])
        candidates = dict.fromkeys(c for c in (
            f"{first2}{last}@{domain}" if first2 and last else None,
            f"{first_full}.{last}@{domain}" if first_full and last else None,
            f"{local}2@{domain}",
        ) if c)
        available = [c for c in candidates if c != upn and not client.get_user(c, "id")]
        if available:
            print("Available alternates:")
            for candidate in available:
                print(f"  {candidate}")
        raise ProvisionError("pick a UPN and re-run with --upn")

    password = temp_password()
    display_name = f"{hire['first_name']} {hire['last_name']}"
    payload = {
        "accountEnabled": True,
        "displayName": display_name,
        "givenName": hire["first_name"],
        "surname": hire["last_name"],
        "userPrincipalName": upn,
        "mailNickname": upn.split("@", 1)[0],
        "usageLocation": (config.get("tenant") or {}).get("usage_location", "US"),
        "passwordProfile": {
            "password": password,
            "forceChangePasswordNextSignIn": True,
        },
    }
    if hire.get("title"):
        payload["jobTitle"] = hire["title"]
    if hire.get("property_name"):
        payload["officeLocation"] = hire["property_name"]

    try:
        created = client.create_user(payload)
    except GraphError as exc:
        if exc.status == 400 and "userPrincipalName already exists" in str(exc):
            # The pre-check can miss an account created seconds ago — the
            # directory lags a little before new UPNs are readable.
            raise ProvisionError(
                f"{upn} already exists — if it was just created, the directory "
                "can lag a few seconds; re-run to see it, or pick another UPN "
                "with --upn"
            ) from exc
        raise
    print(f"Created {display_name}  ({created.get('userPrincipalName', upn)})")
    print(f"  temp password: {password}  (must change at first sign-in)")
    print("  license + groups come in Phase 3 — assign manually for now.")


def cmd_reuse(args):
    config = load_config()
    hire = load_hire()
    client = GraphClient.from_env()

    target = args.upn or hire.get("reuse_upn")
    if not target:
        raise ProvisionError(
            "reuse needs the role account — set reuse_upn in hire.yaml or pass --upn"
        )
    domain = tenant_domain(config, required=False)
    upn = target if "@" in target or not domain else f"{target}@{domain}"

    user = client.get_user(upn, USER_FIELDS)
    if not user:
        raise ProvisionError(f"{upn} not found — run discover first to see what exists")

    old_status = "enabled" if user.get("accountEnabled") else "disabled"
    print(f"Reusing {upn} (was: {user.get('displayName')}, {old_status})")

    # Lock the departed employee out first: new password, then kill sessions.
    # If the password step is denied, nothing has been changed yet.
    password = temp_password()
    try:
        client.update_user(user["id"], {
            "passwordProfile": {
                "password": password,
                "forceChangePasswordNextSignIn": True,
            },
        })
    except GraphError as exc:
        if exc.status == 403:
            raise ProvisionError(
                "password reset was denied — app-only password changes need the "
                "User-PasswordProfile.ReadWrite.All application permission "
                "(admin-consented); User.ReadWrite.All alone doesn't cover them. "
                "Nothing was changed."
            ) from exc
        raise
    client.revoke_sessions(user["id"])

    changes = {
        "accountEnabled": True,
        "displayName": f"{hire['first_name']} {hire['last_name']}",
        "givenName": hire["first_name"],
        "surname": hire["last_name"],
    }
    if hire.get("title"):
        changes["jobTitle"] = hire["title"]
    client.update_user(user["id"], changes)

    print(f"  now: {changes['displayName']} — password reset, sessions revoked, account enabled")
    print(f"  temp password: {password}  (must change at first sign-in)")
    print("  mailbox history stays with the role account.")


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

    discover = subparsers.add_parser(
        "discover", help="look up existing accounts before deciding new vs. reuse"
    )
    discover.add_argument(
        "target", help="property number (checks each role prefix) or a UPN prefix"
    )
    discover.set_defaults(func=cmd_discover)

    new = subparsers.add_parser(
        "new", help="create a fresh account for the hire in hire.yaml"
    )
    new.add_argument("--upn", help="override the default first-initial+last-name UPN")
    new.set_defaults(func=cmd_new)

    reuse = subparsers.add_parser(
        "reuse", help="hand an existing role account to the hire in hire.yaml"
    )
    reuse.add_argument("--upn", help="role account to reuse (defaults to reuse_upn in hire.yaml)")
    reuse.set_defaults(func=cmd_reuse)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, GraphError, ProvisionError) as exc:
        sys.stdout.flush()  # keep any printed detail ahead of the error line
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
