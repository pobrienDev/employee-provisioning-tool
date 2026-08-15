#!/usr/bin/env python3
"""Entra ID provisioning tool — automates new-hire setup and offboarding.

Commands:
  list-users            all users in the tenant (auth smoke test)
  discover 619          role accounts for a property number
  discover manager619   accounts matching a UPN prefix
  new                   create a fresh account for the hire in hire.yaml
  reuse                 hand an existing role account to the hire in hire.yaml
  terminate <upn>       offboard: disable, revoke sessions, strip groups/licenses
  skus                  list license SKU IDs for config.yaml

new, reuse, and terminate accept --dry-run: reads still hit the API so the
output is realistic, but every write is replaced with a "[dry-run] would ..."
line. Every action is appended to logs/provision-<date>.log — actions only,
never passwords or personal contact details.
"""

import argparse
import secrets
import string
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

from graph_api import AUTH_METHOD_PATHS, ConfigError, GraphClient, GraphError

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
USER_FIELDS = "id,displayName,userPrincipalName,accountEnabled,jobTitle,officeLocation"


class ProvisionError(Exception):
    """A provisioning step can't proceed; the message says why."""


def audit(message):
    """Append a timestamped line to today's audit log.

    Actions only — passwords and personal contact details never go in.
    """
    LOG_DIR.mkdir(exist_ok=True)
    now = datetime.now()
    path = LOG_DIR / f"provision-{now:%Y-%m-%d}.log"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now:%Y-%m-%d %H:%M:%S}  {message}\n")


def act(message):
    """Print an action line and record it in the audit log."""
    print(f"  {message}")
    audit(message)


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


def provision_extras(client, config, hire, user_id, dry):
    """License and property-group membership, shared by new and reuse.

    Returns a list of issues rather than raising, so one failure doesn't
    abandon the remaining steps.
    """
    issues = []

    sku = config.get("license_sku")
    if not sku:
        act("license skipped — no license_sku in config.yaml")
    elif dry:
        act(f"[dry-run] would assign license {sku}")
    else:
        try:
            client.assign_license(user_id, sku)
            act("license assigned")
        except GraphError as exc:
            if "available licenses" in str(exc):
                msg = "license not assigned — no seats left on the configured SKU"
            else:
                msg = f"license not assigned — {exc}"
            act(msg)
            issues.append(msg)

    prop = str(hire.get("property_number") or "")
    if not prop:
        act("groups skipped — no property_number in hire.yaml")
        return issues
    properties = {str(k): v for k, v in (config.get("properties") or {}).items()}
    mapping = properties.get(prop)
    if not isinstance(mapping, dict) or not mapping.get("groups"):
        act(f"groups skipped — property {prop} has no groups in config.yaml")
        return issues

    for group_id in mapping["groups"]:
        group = client.get_group(group_id)
        if group is None:
            msg = f"group {group_id} not found — check config.yaml"
            act(msg)
            issues.append(msg)
            continue
        label = group.get("displayName") or group_id
        if dry:
            act(f"[dry-run] would add to group: {label}")
            continue
        try:
            client.add_group_member(group_id, user_id)
            act(f"added to group: {label}")
        except GraphError as exc:
            if exc.status == 400 and "already exist" in str(exc):
                act(f"already in group: {label}")
            else:
                msg = f"could not add to {label}: {exc}"
                act(msg)
                issues.append(msg)
    return issues


def reset_mfa(client, user_id, dry):
    """Remove the account's registered MFA methods (previous holder's phone,
    Authenticator, security keys) so the next owner enrolls fresh.

    Used by reuse only: a terminated account is disabled outright, so its
    registrations are left untouched. Returns a list of issues; a missing
    permission degrades to a note.
    """
    try:
        methods = client.list_auth_methods(user_id)
    except GraphError as exc:
        if exc.status == 403:
            act(
                "mfa reset skipped — needs the UserAuthenticationMethod."
                "ReadWrite.All application permission (admin-consented)"
            )
            return []
        raise

    removable = [m for m in methods if m.get("@odata.type") in AUTH_METHOD_PATHS]
    if not removable:
        act("no registered mfa methods to remove")
        return []
    if dry:
        act(f"[dry-run] would remove {len(removable)} registered mfa method(s)")
        return []

    issues = []
    for method in removable:
        path = AUTH_METHOD_PATHS[method["@odata.type"]]
        label = path.removesuffix("Methods")
        try:
            client.delete_auth_method(user_id, path, method["id"])
            act(f"removed mfa method: {label}")
        except GraphError as exc:
            msg = f"could not remove mfa method {label}: {exc}"
            act(msg)
            issues.append(msg)
    return issues


def checklist(hire):
    """Reminder list of the non-M365 platforms marked yes on the form."""
    platforms = hire.get("platforms")
    if not isinstance(platforms, dict):
        return
    needed = [str(name) for name, wanted in platforms.items() if wanted]
    if needed:
        act("manual checklist — accounts still to create: " + ", ".join(needed))


def email_draft(hire, display_name, upn, password):
    """Print a ready-to-paste login-info email. Shown once, never saved."""
    to = hire.get("login_info_email")
    if not to:
        return
    cc = hire.get("rpm_email") if hire.get("copy_rpm") else None

    print("\n--- login-info email draft (copy into your mail client; not sent, not saved) ---")
    print(f"To: {to}")
    if cc:
        print(f"Cc: {cc}")
    print(f"Subject: Login details for {display_name}")
    print()
    print(f"{display_name}'s account is ready.")
    print()
    print(f"  Username:           {upn}")
    print(f"  Temporary password: {password or '(generated at the real run)'}")
    print()
    print("They'll be prompted to choose a new password at first sign-in.")
    print("--- end draft ---")
    audit(f"login-info email drafted for {upn}" + (" (cc RPM)" if cc else ""))


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


def cmd_skus(args):
    client = GraphClient.from_env()
    try:
        skus = client.list_skus()
    except GraphError as exc:
        if exc.status == 403:
            raise ProvisionError(
                "listing licenses needs the Organization.Read.All application "
                "permission (admin-consented)"
            ) from exc
        raise
    if not skus:
        print("No license SKUs in this tenant.")
        return
    print(f"{len(skus)} SKU(s):\n")
    for sku in skus:
        enabled = (sku.get("prepaidUnits") or {}).get("enabled", 0)
        used = sku.get("consumedUnits", 0)
        part = sku.get("skuPartNumber") or "?"
        print(f"  {part:<32}  {sku.get('skuId')}  ({used}/{enabled} used)")


def cmd_new(args):
    config = load_config()
    hire = load_hire()
    client = GraphClient.from_env()
    dry = args.dry_run
    upn = build_upn(hire, config, args.upn)
    audit(f"new: {upn}{' (dry-run)' if dry else ''}")

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

    display_name = f"{hire['first_name']} {hire['last_name']}"
    if dry:
        act(f"[dry-run] would create {display_name} ({upn}) with a temporary must-change password")
        password = None
        user_id = None
    else:
        password = temp_password()
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
        if hire.get("property_number"):
            payload["department"] = str(hire["property_number"])

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
        act(f"created {display_name} ({created.get('userPrincipalName', upn)})")
        print(f"  temp password: {password}  (must change at first sign-in)")
        user_id = created["id"]

    issues = provision_extras(client, config, hire, user_id, dry)
    checklist(hire)
    email_draft(hire, display_name, upn, password)
    if issues:
        raise ProvisionError("completed with issues: " + "; ".join(issues))


def cmd_reuse(args):
    config = load_config()
    hire = load_hire()
    client = GraphClient.from_env()
    dry = args.dry_run

    target = args.upn or hire.get("reuse_upn")
    if not target:
        raise ProvisionError(
            "reuse needs the role account — set reuse_upn in hire.yaml or pass --upn"
        )
    domain = tenant_domain(config, required=False)
    upn = target if "@" in target or not domain else f"{target}@{domain}"
    audit(f"reuse: {upn}{' (dry-run)' if dry else ''}")

    user = client.get_user(upn, USER_FIELDS)
    if not user:
        raise ProvisionError(f"{upn} not found — run discover first to see what exists")

    old_status = "enabled" if user.get("accountEnabled") else "disabled"
    print(f"Reusing {upn} (was: {user.get('displayName')}, {old_status})")

    display_name = f"{hire['first_name']} {hire['last_name']}"
    if dry:
        act(
            f"[dry-run] would reset the password, revoke sessions, rename to "
            f"{display_name}, and enable the account"
        )
        password = None
    else:
        # Lock the departed employee out first: new password, then kill
        # sessions. If the password step is denied, nothing has changed yet.
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
            "displayName": display_name,
            "givenName": hire["first_name"],
            "surname": hire["last_name"],
            # assignLicense requires a usageLocation; older role accounts may lack one
            "usageLocation": (config.get("tenant") or {}).get("usage_location", "US"),
        }
        if hire.get("title"):
            changes["jobTitle"] = hire["title"]
        if hire.get("property_number"):
            changes["department"] = str(hire["property_number"])
        client.update_user(user["id"], changes)

        act(f"now: {display_name} — password reset, sessions revoked, account enabled")
        print(f"  temp password: {password}  (must change at first sign-in)")
        print("  mailbox history stays with the role account.")

    issues = reset_mfa(client, user["id"], dry)
    issues += provision_extras(client, config, hire, user["id"], dry)
    checklist(hire)
    email_draft(hire, display_name, upn, password)
    if issues:
        raise ProvisionError("completed with issues: " + "; ".join(issues))


def cmd_terminate(args):
    config = load_config()
    client = GraphClient.from_env()
    dry = args.dry_run
    domain = tenant_domain(config, required=False)
    upn = args.upn if "@" in args.upn or not domain else f"{args.upn}@{domain}"
    audit(f"terminate: {upn}{' (dry-run)' if dry else ''}")

    user = client.get_user(upn, USER_FIELDS + ",assignedLicenses")
    if not user:
        raise ProvisionError(
            f"{upn} not found — note that accounts created moments ago can "
            "take a few seconds to become visible"
        )

    groups = client.get_member_groups(user["id"])
    licenses = [lic["skuId"] for lic in user.get("assignedLicenses") or []]

    print("Terminating:")
    print_user(user)

    if dry:
        act("[dry-run] would disable the account and revoke every session")
        for group in groups:
            act(f"[dry-run] would remove from group: {group.get('displayName') or group['id']}")
        if licenses:
            act(f"[dry-run] would remove {len(licenses)} license(s)")
        else:
            act("no licenses to remove")
        print("  manual step if mail must be retained: convert the mailbox to shared (Exchange admin center)")
        return

    print(
        f"  plan: disable account, revoke sessions, remove "
        f"{len(groups)} group membership(s), remove {len(licenses)} license(s)"
    )
    if not args.yes:
        raise ProvisionError("nothing done — re-run with --yes to offboard this account")

    # Lock out first, then clean up.
    client.update_user(user["id"], {"accountEnabled": False})
    client.revoke_sessions(user["id"])
    act("account disabled, sessions revoked")

    issues = []
    for group in groups:
        label = group.get("displayName") or group["id"]
        try:
            client.remove_group_member(group["id"], user["id"])
            act(f"removed from group: {label}")
        except GraphError as exc:
            msg = f"could not remove from {label}: {exc}"
            act(msg)
            issues.append(msg)

    if licenses:
        client.remove_licenses(user["id"], licenses)
        act(f"removed {len(licenses)} license(s)")
    else:
        act("no licenses to remove")

    print("  manual step if mail must be retained: convert the mailbox to shared (Exchange admin center)")
    if issues:
        raise ProvisionError("offboarding finished with issues — " + "; ".join(issues))


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
    new.add_argument(
        "--dry-run", action="store_true",
        help="print what would happen without changing anything",
    )
    new.set_defaults(func=cmd_new)

    reuse = subparsers.add_parser(
        "reuse", help="hand an existing role account to the hire in hire.yaml"
    )
    reuse.add_argument("--upn", help="role account to reuse (defaults to reuse_upn in hire.yaml)")
    reuse.add_argument(
        "--dry-run", action="store_true",
        help="print what would happen without changing anything",
    )
    reuse.set_defaults(func=cmd_reuse)

    skus = subparsers.add_parser(
        "skus", help="list license SKUs and their IDs (for license_sku in config.yaml)"
    )
    skus.set_defaults(func=cmd_skus)

    terminate = subparsers.add_parser(
        "terminate", help="offboard an account: disable, revoke sessions, strip groups and licenses"
    )
    terminate.add_argument("upn", help="the account to offboard (domain appended if omitted)")
    terminate.add_argument(
        "--yes", action="store_true",
        help="actually offboard; without it the command only previews the plan",
    )
    terminate.add_argument(
        "--dry-run", action="store_true",
        help="print what would happen without changing anything",
    )
    terminate.set_defaults(func=cmd_terminate)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, GraphError, ProvisionError) as exc:
        sys.stdout.flush()  # keep any printed detail ahead of the error line
        print(f"error: {exc}", file=sys.stderr)
        audit(f"error ({args.command}): {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
