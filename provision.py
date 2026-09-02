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
import html
import re
import secrets
import shutil
import string
import subprocess
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


def enrich_from_property(hire, config):
    """Fill in hire fields the config property list can supply when absent.

    property_name comes from the property entry's name and rpm_email from
    its rpm, so hire.yaml only needs the property number once config.yaml
    knows the property. Explicit values in hire.yaml still win.
    """
    if not hire.get("property_number"):
        return hire
    properties = {str(k): v for k, v in (config.get("properties") or {}).items()}
    entry = properties.get(str(hire["property_number"]))
    if not isinstance(entry, dict):
        return hire
    hire = dict(hire)
    if not hire.get("property_name") and entry.get("name"):
        hire["property_name"] = entry["name"]
    if not hire.get("rpm_email") and entry.get("rpm"):
        hire["rpm_email"] = entry["rpm"]
    return hire


def display_title(hire, config):
    """The hire's title as it should read on the account.

    Forms don't always word a title the way it should read in a display
    name, so naming.title_display maps the form's wording to the display
    one (e.g. "Concierge/Leasing" -> "Leasing Concierge"). It affects the
    display name alone: the Job title attribute records the form's exact
    wording, as do group and licensing matching. Unmapped titles pass
    through.
    """
    title = (hire.get("title") or "").strip()
    mapping = (config.get("naming") or {}).get("title_display") or {}
    for form_wording, shown in mapping.items():
        if str(form_wording).strip().lower() == title.lower():
            return str(shown)
    return title


def display_name_for(hire, config):
    """Role-style display name at properties, personal name at corporate.

    Accounts at a property display as "{title} at {property name}"; the
    corporate property (corporate_property in config.yaml, default "50")
    keeps "First Last". Falls back to the personal name when title or
    property name is missing.
    """
    prop = str(hire.get("property_number") or "")
    corporate = str(config.get("corporate_property") or "50")
    if prop and prop != corporate and hire.get("title") and hire.get("property_name"):
        return f"{display_title(hire, config)} at {hire['property_name']}"
    return f"{hire['first_name']} {hire['last_name']}"


def pick_upn(client, hire, config):
    """Find the first available UPN for the hire's name.

    Tries first initial + last name, then two letters of the first name,
    three, and so on through the full first name; falls back to numbered
    variants if every letter-based one is taken.
    """
    domain = tenant_domain(config)
    first = sanitize_local(hire["first_name"])
    last = sanitize_local(hire["last_name"])
    if not first and not last:
        raise ProvisionError("could not build a UPN from the hire's name — pass --upn")
    if first and last:
        stems = [f"{first[:i]}{last}" for i in range(1, len(first) + 1)]
    else:
        stems = [first or last]
    stems = list(dict.fromkeys(stems))
    candidates = stems + [f"{stems[-1]}{n}" for n in range(2, 10)]

    for local in candidates:
        upn = f"{local}@{domain}"
        existing = client.get_user(upn, "id,displayName")
        if existing is None:
            return upn
        holder = existing.get("displayName") or "existing account"
        print(f"  {upn} taken ({holder}) — trying next")
    raise ProvisionError("no available UPN found after trying every variant — pass --upn")


def groups_for(hire, config):
    """Every group the hire should join, from three sources:

    - the property's own groups (properties.<number>.groups)
    - corporate or site membership (groups.corporate / groups.site,
      chosen by comparing property_number to corporate_property)
    - the job title (groups.titles, case-insensitive exact match)

    Duplicates are collapsed, order preserved.
    """
    selected = []
    prop = str(hire.get("property_number") or "")
    corporate = str(config.get("corporate_property") or "50")
    group_cfg = config.get("groups") or {}

    if prop:
        properties = {str(k): v for k, v in (config.get("properties") or {}).items()}
        mapping = properties.get(prop)
        if isinstance(mapping, dict):
            selected += mapping.get("groups") or []
        selected += group_cfg.get("corporate" if prop == corporate else "site") or []

    title = (hire.get("title") or "").strip().lower()
    if title:
        for name, ids in (group_cfg.get("titles") or {}).items():
            if str(name).strip().lower() == title:
                selected += ids or []

    return list(dict.fromkeys(selected))


def choose_license(client, config, hire):
    """Pick the license SKU for this hire from config.yaml's licensing rules.

    The `licensing` section holds ordered fallback chains — corporate
    (property_number == corporate_property), maintenance (title contains a
    maintenance keyword), and default — whose entries are skuPartNumber
    values or skuId GUIDs. The first SKU in the hire's chain with free seats
    wins; a chain with one entry means "this SKU or nothing". Without a
    `licensing` section, the flat `license_sku` keeps its old behavior.

    Returns (sku_id, label, notes, problem): sku_id is None when nothing
    should be assigned, notes are act()-ready lines explaining the choice,
    and problem is set when the outcome should count as an issue.
    """
    licensing = config.get("licensing")
    if not isinstance(licensing, dict):
        sku = config.get("license_sku")
        if not sku:
            return None, None, ["license skipped — no licensing rules in config.yaml"], None
        return sku, str(sku), [], None

    prop = str(hire.get("property_number") or "")
    corporate = str(config.get("corporate_property") or "50")
    title = (hire.get("title") or "").lower()
    keywords = [str(k).lower() for k in licensing.get("maintenance_keywords") or ["maintenance"]]

    if prop and prop == corporate:
        chain, which = licensing.get("corporate"), "corporate"
    elif any(keyword in title for keyword in keywords):
        chain, which = licensing.get("maintenance"), "maintenance"
    else:
        chain, which = licensing.get("default"), "default"
    if not chain:
        return None, None, [f"license skipped — no licensing.{which} chain in config.yaml"], None

    try:
        skus = client.list_skus()
    except GraphError as exc:
        if exc.status == 403:
            problem = (
                "license not assigned — checking seat availability needs the "
                "Organization.Read.All application permission (admin-consented)"
            )
            return None, None, [], problem
        raise
    by_key = {}
    for sku in skus:
        by_key[str(sku.get("skuId", "")).lower()] = sku
        by_key[str(sku.get("skuPartNumber", "")).lower()] = sku

    notes = []
    for entry in chain:
        sku = by_key.get(str(entry).lower())
        if sku is None:
            notes.append(f"license option {entry} not in this tenant — skipping it")
            continue
        part = sku.get("skuPartNumber") or sku.get("skuId")
        free = (sku.get("prepaidUnits") or {}).get("enabled", 0) - sku.get("consumedUnits", 0)
        if free <= 0:
            notes.append(f"no {part} seats free — trying next option")
            continue
        return sku["skuId"], f"{part} ({which} rule, {free} seat(s) free)", notes, None

    problem = (
        f"license not assigned — no seats free on any licensing.{which} option "
        f"({', '.join(str(entry) for entry in chain)})"
    )
    return None, None, notes, problem


def provision_extras(client, config, hire, user_id, dry, upn=None, join_dls=False):
    """License and group membership, shared by new and reuse.

    Distribution lists print as manual steps unless join_dls asks for the
    Exchange Online PowerShell path. Returns a list of issues rather than
    raising, so one failure doesn't abandon the remaining steps.
    """
    issues = []

    sku_id, label, notes, problem = choose_license(client, config, hire)
    for note in notes:
        act(note)
    if problem:
        act(problem)
        issues.append(problem)
    elif sku_id is None:
        pass  # the notes already said why nothing gets assigned
    elif dry:
        act(f"[dry-run] would assign license {label}")
    else:
        try:
            client.assign_license(user_id, sku_id)
            act(f"license assigned: {label}")
        except GraphError as exc:
            if "available licenses" in str(exc):
                msg = "license not assigned — the last free seat was taken mid-run"
            else:
                msg = f"license not assigned — {exc}"
            act(msg)
            issues.append(msg)

    group_ids = groups_for(hire, config)
    if not group_ids:
        act("groups skipped — nothing mapped for this property or title in config.yaml")
        return issues

    pending_dls = []
    for group_id in group_ids:
        group = client.get_group(
            group_id, "displayName,groupTypes,mailEnabled,securityEnabled,mail"
        )
        if group is None:
            msg = f"group {group_id} not found — check config.yaml"
            act(msg)
            issues.append(msg)
            continue
        label = group.get("displayName") or group_id
        unified = "Unified" in (group.get("groupTypes") or [])
        if group.get("mailEnabled") and not unified:
            # Classic Exchange groups (distribution lists and mail-enabled
            # security groups) are read-only through the Graph API — their
            # membership lives in Exchange. Joined via Exchange Online
            # PowerShell behind --join-dls, otherwise printed as paste-ready
            # commands. The SMTP address is the identity Exchange resolves
            # unambiguously; the object ID is only a fallback.
            pending_dls.append((group.get("mail") or group_id, label))
            continue
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

    if pending_dls:
        labels = ", ".join(label for _, label in pending_dls)
        if join_dls and upn:
            if dry:
                act(
                    f"[dry-run] would join {len(pending_dls)} distribution "
                    f"list(s) via Exchange Online PowerShell, signed in as you: {labels}"
                )
            else:
                try:
                    issues += join_distribution_lists(upn, pending_dls)
                except ProvisionError as exc:
                    act(str(exc))
                    issues.append(str(exc))
        else:
            # Paste-ready commands beat a per-run sign-in: connect once per
            # day, then each hire's joins are a two-second paste. (The
            # admin center's Assign memberships panel works too.)
            act(
                f"{len(pending_dls)} distribution list join(s) printed below — "
                "paste into an Exchange Online PowerShell window "
                "(Connect-ExchangeOnline once, then reuse the session)"
            )
            member = (upn or "<upn>").replace("'", "''")
            for identity, label in pending_dls:
                quoted = str(identity).replace("'", "''")
                print(
                    f"    Add-DistributionGroupMember -Identity '{quoted}' "
                    f"-Member '{member}'  # {label}"
                )
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


def role_alias_note(client, hire, config, upn):
    """Suggest a personal email alias for role-format accounts.

    A UPN like manager536@ still belongs to a person, so mail should also
    reach them at {first initial}{last name}@. Graph can't write Exchange
    aliases (proxyAddresses is read-only), so the tool picks the first free
    personal address and prints it as a manual step.
    """
    local = upn.split("@", 1)[0]
    roles = (config.get("naming") or {}).get("roles") or []
    if not any(local.startswith(role) and local[len(role):].isdigit() for role in roles):
        return
    try:
        alias = pick_upn(client, hire, config)
    except ProvisionError:
        return
    act(
        f"manual step — add {alias} as an email alias for {upn} "
        "(admin center → user → Manage username and aliases; the Graph API can't set aliases)"
    )


def checklist(hire):
    """Reminder list of the non-M365 platforms marked yes on the form."""
    platforms = hire.get("platforms")
    if not isinstance(platforms, dict):
        return
    needed = [str(name) for name, wanted in platforms.items() if wanted]
    if needed:
        act("manual checklist — accounts still to create: " + ", ".join(needed))


def copy_draft_to_clipboard(body):
    """Put the draft body on the clipboard as rich text (best-effort).

    A URL pasted into Outlook as plain text stays plain — Outlook only
    links URLs as they are typed. Copying the body as HTML keeps the link
    clickable and the line breaks intact. On failure the printed draft is
    still there to copy by hand, so this never raises.
    """
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        return False
    linked = re.sub(
        r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', html.escape(body)
    )
    body_html = (
        '<div style="font-family:Calibri, Arial, sans-serif; font-size:11pt">'
        + linked.replace("\n", "<br>\n")
        + "</div>"
    )
    # The body travels over stdin as UTF-8 bytes: nothing sensitive lands
    # on the command line, and no console codepage can garble it.
    script = (
        "$in=[Console]::OpenStandardInput(); "
        "$ms=New-Object IO.MemoryStream; $in.CopyTo($ms); "
        "Set-Clipboard -AsHtml -Value ([Text.Encoding]::UTF8.GetString($ms.ToArray()))"
    )
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-Command", script],
            input=body_html.encode("utf-8"), capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def email_draft(hire, display_name, upn, password):
    """Print a ready-to-paste login-info email. Shown once, never saved.

    The wording lives in email_template.txt (git-ignored, so it can carry
    company-specific text), falling back to the committed
    email_template.example.txt. Placeholders: {name}, {first}, {last},
    {username}, {password}.
    """
    to = hire.get("login_info_email")
    if not to:
        return
    cc = hire.get("rpm_email") if hire.get("copy_rpm") else None

    source = next(
        (name for name in ("email_template.txt", "email_template.example.txt")
         if (BASE_DIR / name).exists()),
        None,
    )
    if source is None:
        act("email draft skipped — no email_template.txt found")
        return
    template = (BASE_DIR / source).read_text(encoding="utf-8")
    try:
        rendered = template.format(
            name=display_name,
            first=hire["first_name"],
            last=hire["last_name"],
            username=upn,
            password=password or "(generated at the real run)",
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise ProvisionError(
            f"{source} has a placeholder problem ({exc}) — allowed: "
            "{name}, {first}, {last}, {username}, {password}"
        ) from exc

    print("\n--- login-info email draft (copy into your mail client; not sent, not saved) ---")
    print(f"To: {to}")
    if cc:
        print(f"Cc: {cc}")
    print(rendered.rstrip())
    print("--- end draft ---")

    # First template line is the subject; the clipboard gets just the body.
    parts = rendered.rstrip().split("\n", 1)
    body = parts[1].lstrip("\n") if len(parts) > 1 else parts[0]
    if copy_draft_to_clipboard(body):
        print("  (body copied to the clipboard as rich text — paste into Outlook and the link stays clickable)")
    else:
        print("  (clipboard copy unavailable — after pasting in Outlook, click at the end of the link and press Enter to make it clickable)")
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
    hire = enrich_from_property(load_hire(), config)
    client = GraphClient.from_env()
    dry = args.dry_run
    if args.upn:
        # An explicit UPN is a decision, not a starting point — never
        # silently substitute a different one for it.
        domain = tenant_domain(config)
        upn = args.upn if "@" in args.upn else f"{args.upn}@{domain}"
        existing = client.get_user(upn, USER_FIELDS)
        if existing:
            print(f"{upn} already exists:")
            print_user(existing)
            raise ProvisionError("that UPN is taken — pick another with --upn")
    else:
        upn = pick_upn(client, hire, config)
    audit(f"new: {upn}{' (dry-run)' if dry else ''}")

    display_name = display_name_for(hire, config)
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

    issues = provision_extras(
        client, config, hire, user_id, dry, upn=upn, join_dls=args.join_dls
    )
    role_alias_note(client, hire, config, upn)
    checklist(hire)
    email_draft(hire, f"{hire['first_name']} {hire['last_name']}", upn, password)
    if issues:
        raise ProvisionError("completed with issues: " + "; ".join(issues))


def cmd_reuse(args):
    config = load_config()
    hire = enrich_from_property(load_hire(), config)
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

    display_name = display_name_for(hire, config)
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
    issues += provision_extras(
        client, config, hire, user["id"], dry, upn=upn, join_dls=args.join_dls
    )
    role_alias_note(client, hire, config, upn)
    checklist(hire)
    email_draft(hire, f"{hire['first_name']} {hire['last_name']}", upn, password)
    if issues:
        raise ProvisionError("completed with issues: " + "; ".join(issues))


def exchange_shell(body):
    """Run commands inside an Exchange Online PowerShell session.

    Membership of classic distribution lists and mailbox type are Exchange
    settings the Graph API can't write, so those steps shell out to the
    ExchangeOnlineManagement module — the only place the tool acts as the
    signed-in operator rather than the app registration, and always behind
    an opt-in flag. Connect-ExchangeOnline opens a sign-in prompt; the
    operator needs an Exchange admin (or recipient management) role.
    """
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        raise ProvisionError("no PowerShell found — Exchange steps need it")
    script = (
        "Import-Module ExchangeOnlineManagement -ErrorAction Stop; "
        "Connect-ExchangeOnline -ShowBanner:$false; "
        + body +
        "; Disconnect-ExchangeOnline -Confirm:$false"
    )
    try:
        return subprocess.run(
            [shell, "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProvisionError(
            "Exchange Online PowerShell timed out — complete the sign-in "
            "prompt, or do the step manually in the admin center"
        ) from exc


def convert_mailbox_shared(upn):
    """Convert the account's mailbox to a shared mailbox (terminate's
    opt-in --convert-shared)."""
    result = exchange_shell(f"Set-Mailbox -Identity '{upn}' -Type Shared -ErrorAction Stop")
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        detail = lines[-1].strip() if lines else f"exit code {result.returncode}"
        raise ProvisionError(
            f"mailbox conversion failed — {detail} (is the "
            "ExchangeOnlineManagement module installed, and do you hold an "
            "Exchange admin role?)"
        )


def join_distribution_lists(upn, dls):
    """Add the account to classic distribution lists (new/reuse's opt-in
    --join-dls), all in one Exchange session.

    dls is a list of (identity, label) pairs where identity is the group's
    SMTP address — the identity Exchange resolves unambiguously — with the
    object ID as a fallback. Already-a-member counts as joined. Returns a
    list of issues; each outcome is reported through act().
    """
    quoted_upn = upn.replace("'", "''")
    body = "; ".join(
        f"try {{ Add-DistributionGroupMember -Identity '{gid}' "
        f"-Member '{quoted_upn}' -ErrorAction Stop; Write-Output 'JOINED {gid}' }} "
        f"catch {{ if (\"$_\" -match 'already a member') "
        f"{{ Write-Output 'JOINED {gid}' }} else "
        f"{{ Write-Output ('FAILED {gid} ' + $_) }} }}"
        for gid, _ in dls
    )
    result = exchange_shell(body)
    out = result.stdout or ""

    if result.returncode != 0 and "JOINED" not in out and "FAILED" not in out:
        lines = (result.stderr or out).strip().splitlines()
        detail = lines[-1].strip() if lines else f"exit code {result.returncode}"
        msg = (
            f"distribution list joins failed — {detail} (is the "
            "ExchangeOnlineManagement module installed, and do you hold an "
            "Exchange admin or recipient management role?)"
        )
        act(msg)
        return [msg]

    issues = []
    for gid, label in dls:
        if f"JOINED {gid}" in out:
            act(f"added to distribution list: {label}")
            continue
        line = next(
            (ln for ln in out.splitlines() if ln.startswith(f"FAILED {gid}")), ""
        )
        detail = line.partition(f"FAILED {gid}")[2].strip() or "unknown error"
        msg = f"could not add to {label}: {detail}"
        act(msg)
        issues.append(msg)
    return issues


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
        if args.convert_shared:
            act(
                "[dry-run] would convert the mailbox to shared "
                "(Exchange Online PowerShell, signed in as you)"
            )
        for group in groups:
            act(f"[dry-run] would remove from group: {group.get('displayName') or group['id']}")
        if licenses:
            act(f"[dry-run] would remove {len(licenses)} license(s)")
        else:
            act("no licenses to remove")
        if not args.convert_shared:
            print("  manual step if mail must be retained: convert the mailbox to shared (Exchange admin center)")
        return

    print(
        f"  plan: disable account, revoke sessions,"
        f"{' convert the mailbox to shared,' if args.convert_shared else ''} remove "
        f"{len(groups)} group membership(s), remove {len(licenses)} license(s)"
    )
    if not args.yes:
        raise ProvisionError("nothing done — re-run with --yes to offboard this account")

    # Lock out first, then clean up.
    client.update_user(user["id"], {"accountEnabled": False})
    client.revoke_sessions(user["id"])
    act("account disabled, sessions revoked")

    issues = []
    converted = not args.convert_shared  # nothing to wait on when not asked for
    if args.convert_shared:
        # Convert while the mailbox is still licensed; a shared mailbox under
        # 50GB then needs no license of its own.
        try:
            convert_mailbox_shared(upn)
            act("mailbox converted to shared")
            converted = True
        except ProvisionError as exc:
            act(str(exc))
            issues.append(str(exc))

    for group in groups:
        label = group.get("displayName") or group["id"]
        try:
            client.remove_group_member(group["id"], user["id"])
            act(f"removed from group: {label}")
        except GraphError as exc:
            msg = f"could not remove from {label}: {exc}"
            act(msg)
            issues.append(msg)

    if not licenses:
        act("no licenses to remove")
    elif converted:
        client.remove_licenses(user["id"], licenses)
        act(f"removed {len(licenses)} license(s)")
    else:
        # Pulling the license off an unconverted mailbox starts its deletion
        # clock — keep it until the conversion has actually happened.
        msg = "licenses kept — convert the mailbox first, then remove them"
        act(msg)
        issues.append(msg)

    if not args.convert_shared:
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
    new.add_argument(
        "--join-dls", action="store_true",
        help="also join Exchange distribution lists via Exchange Online "
             "PowerShell (prompts for your own sign-in; needs an Exchange "
             "admin or recipient management role) instead of printing them "
             "as manual steps",
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
    reuse.add_argument(
        "--join-dls", action="store_true",
        help="also join Exchange distribution lists via Exchange Online "
             "PowerShell (prompts for your own sign-in; needs an Exchange "
             "admin or recipient management role) instead of printing them "
             "as manual steps",
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
        "--convert-shared", action="store_true",
        help="also convert the mailbox to a shared mailbox before removing "
             "licenses (Exchange Online PowerShell — prompts for your own "
             "sign-in and needs an Exchange admin role)",
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
