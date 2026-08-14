# Employee Provisioning Tool

A Python command-line tool that automates employee onboarding and offboarding
in Microsoft Entra ID through the Microsoft Graph API. It turns a new-hire
form submission into account creation (or role-account reuse), license
assignment, group membership, and credential delivery — and turns a
termination into a clean, complete offboarding — with a dry-run mode and an
audit log of every action taken.

```
HR form submission  →  input file  →  provision.py  →  Graph API  →  Entra ID
```

Built for a multi-property organization where account setup was a manual,
multi-step portal process for every hire, and offboarding steps could be
missed — a real security risk. The tool replaces the portal walk-through with
one reviewed command.

## Design

- **App-only auth (client credentials):** the tool authenticates as an Entra
  ID app registration — never as a signed-in user — so it can only ever touch
  the tenant configured in `.env`. Plain `requests` against the REST API; no
  SDK.
- **Scoped permissions:** the app registration carries only the two
  application permissions the tool's core job needs — `User.ReadWrite.All`
  and `Group.ReadWrite.All` — granted once up front so each phase doesn't
  require a new admin-consent round. Anything beyond them (password-profile
  writes, sign-in activity reads) is added only if a phase actually needs it.
- **Secrets stay out of the repo:** credentials live in a git-ignored `.env`,
  tenant-specific IDs in a git-ignored `config.yaml` (the committed
  `config.example.yaml` documents the shape). The `.gitignore` was the
  repository's first commit.
- **Personal data stays out of the repo and the logs:** per-hire input files
  are git-ignored, and the audit log records actions taken, not personal
  details.
- **Reuse or create — human decides:** some hires take over an existing role
  account, others get a fresh personal one. The `discover` command reports the
  role account's status (whose name is on it, enabled/disabled, last sign-in);
  the operator chooses `reuse` or `new`, and the tool does the clicking either
  way.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Client-credentials auth, list users | **done** |
| 2 | `discover` lookup, collision-checked `new`, `reuse` with password reset + session revocation | **done** |
| 3 | License assignment + property group membership (`skus` helper) | **done** |
| 4 | Offboarding: `terminate` — disable, revoke sessions, strip groups and licenses | **done** |
| 5 | `--dry-run`, audit logging, per-hire checklist, login-info email draft | planned |
| 6 | Ticketing-system integration (pull form fields automatically) | stretch |

## Setup

1. **App registration** (in a test tenant while developing):
   Entra admin center → App registrations → New registration. Then under
   *API permissions*, add **application** permissions `User.ReadWrite.All`
   and `Group.ReadWrite.All` (Microsoft Graph) and grant admin consent.
   Under *Certificates & secrets*, create a client secret.

2. **Python environment:**

   ```
   python3 -m venv venv              # Windows: python -m venv venv
   source venv/bin/activate          # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Credentials:** copy `.env.example` to `.env` and fill in the tenant ID,
   client (application) ID, and client secret from the app registration.

4. **Config:** copy `config.example.yaml` to `config.yaml` and fill in the
   tenant domain, the role-account prefixes used at your properties, the
   license SKU to assign (`python provision.py skus` lists the IDs; leave
   blank in a tenant with no licenses and the step is skipped), and the
   property number → group ID mappings.

Optional permissions unlock extras:

- `User-PasswordProfile.ReadWrite.All` — required for the password reset in
  `reuse` (app-only password changes aren't covered by `User.ReadWrite.All`;
  without it, `reuse` stops cleanly before changing anything).
- `AuditLog.Read.All` (plus an Entra ID P1 license) — lets `discover` show
  last sign-in times; without it the column is skipped.
- `Organization.Read.All` — lets `skus` list the tenant's license SKUs and
  their IDs.

## Usage

With the venv activated:

```
python provision.py list-users            # all users — the auth smoke test
python provision.py discover 619          # role accounts for property 619
python provision.py discover manager619   # accounts matching a UPN prefix
python provision.py new                   # fresh account for the hire in hire.yaml
python provision.py new --upn tsmith2     # ...with an explicit UPN
python provision.py reuse                 # hand reuse_upn's account to the hire
python provision.py reuse --upn manager619
python provision.py skus                  # license SKU IDs for config.yaml
python provision.py terminate manager619        # preview the offboarding plan
python provision.py terminate manager619 --yes  # actually offboard
```

`new` builds the UPN from first initial + last name, refuses to overwrite an
existing account (suggesting available alternates instead), and creates the
user with a temporary must-change password. `reuse` locks the departed
employee out first — password reset, then session revocation — before
renaming and re-enabling the account for the new hire.

After either path, the tool assigns the configured license (skipped with a
note when `license_sku` is blank) and joins the account to the groups mapped
to the hire's property, reporting each group by name and treating an
already-present membership as fine. Transient Graph throttling and
concurrency errors are retried automatically.

`terminate` offboards in lockout-first order: disable the account and revoke
every session, then remove all group memberships and licenses. Without
`--yes` it only prints who would be offboarded and what would happen — the
destructive path always requires the flag. Converting the mailbox to shared
(if mail must be retained) is printed as a manual follow-up, since that is an
Exchange operation outside the Graph v1.0 API.

`hire.yaml` (git-ignored) carries the current hire's details:

```yaml
first_name: Taylor
last_name: Example
title: Property Manager
property_number: 619
property_name: Example Apartments
# reuse mode only — the role account being handed over:
reuse_upn: manager619
```
