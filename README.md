# Employee Provisioning Tool

A Python command-line tool that automates employee onboarding and offboarding
in Microsoft Entra ID through the Microsoft Graph API. It turns a Formstack
"Employee Status Change" submission — which arrives as a Zendesk ticket —
into account creation (or role-account reuse), license assignment, group
membership, and credential delivery, and turns a termination into a
lockout-first offboarding of the account, its Graph-managed memberships and
its licenses — with a dry-run mode and an audit log of every action taken.

```
Formstack form (via Zendesk ticket)  →  hire.yaml  →  provision.py  →  Graph API  →  Entra ID
```

Built for a multi-property organization where account setup was a manual,
multi-step portal process for every hire, and offboarding steps could be
missed — a real security risk. The tool replaces the portal walk-through with
one reviewed command.

## Design

- **App-only auth (client credentials) at the core:** the tool authenticates
  as an Entra ID app registration, so it can only ever touch the tenant
  configured in `.env` — the file next to the script wins over any variable
  still exported in the shell, and every write command prints the tenant ID
  and domain it is about to touch before doing anything. Plain `requests`
  against the REST API; no SDK.
  The exceptions are deliberate and opt-in: steps the Graph application
  model can't or shouldn't do (Exchange distribution lists, mailbox
  conversion, drafts in the operator's own mailbox) run as the *signed-in
  operator* behind explicit flags, borrowing the operator's rights for that
  moment instead of enlarging the app's.
- **Scoped permissions:** the app registration carries only the
  least-privileged application permissions Microsoft's Graph reference lists
  for the tool's core job — `User.ReadWrite.All` (create, update and disable
  accounts; it also covers license assignment), `GroupMember.ReadWrite.All`
  (add and remove group members, and read the groups it touches — not
  `Group.ReadWrite.All`, which would let the app create, delete and
  reconfigure every group) and `User.RevokeSessions.All` (the only
  application permission the reference lists for `revokeSignInSessions`) —
  granted once up front so each phase doesn't require a new admin-consent
  round. Anything beyond them (password-profile writes, sign-in activity
  reads, the delegated mailbox scope) is added only if a feature actually
  needs it, and the tenant-wide application version of a permission is never
  taken where a delegated one does the job. The production app registration
  is configured by hand in the Entra admin center; the sandbox app in
  [entra-terraform](https://github.com/pobrienDev/entra-terraform) codifies
  the same permission list against a personal test tenant.
- **Secrets stay out of the repo:** credentials live in a git-ignored `.env`,
  tenant-specific IDs in a git-ignored `config.yaml` (the committed
  `config.example.yaml` documents the shape). The `.gitignore` was the
  repository's first commit.
- **Personal data stays out of the repo; secrets stay out of the logs:**
  per-hire input files are git-ignored, and the audit log records actions
  taken — never passwords or contact details. Names do appear in it (an
  account's display name, UPNs built from names), since they are what an
  action is about. Every line is tagged with the operator's OS user and
  machine, but the log is a local text file the operator can edit; for an
  authoritative who-did-what, Entra's own audit log records every change
  the app makes under the app's identity.
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
| 5 | `--dry-run`, audit logging, per-hire checklist, login-info email draft | **done** |
| 5+ | Extras: rule-based licensing, DL joins (`--join-dls`), shared-mailbox conversion (`--convert-shared`), clipboard-ready email, Outlook draft with captured signature (`--open-draft`) | **done** |
| 6 | Zendesk integration (pull the Formstack fields from the ticket automatically) | stretch |

## Setup

1. **App registration** (in a test tenant while developing):
   Entra admin center → App registrations → New registration. Then under
   *API permissions*, add the **application** permissions
   `User.ReadWrite.All`, `GroupMember.ReadWrite.All` and
   `User.RevokeSessions.All` (Microsoft Graph) and grant admin consent. If
   the group lookups in `new`/`reuse` come back 403, add `Group.Read.All`
   as well. Under *Certificates & secrets*, create a client secret.

2. **Python environment:**

   ```
   python3 -m venv venv              # Windows: python -m venv venv
   source venv/bin/activate          # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

   `requirements.txt` pins exact versions: a tool with tenant-wide write
   access shouldn't pick up whatever release is newest on install day.
   Dependabot (`.github/dependabot.yml`) proposes upgrades as pull
   requests, each run through the test suite before merging.

3. **Credentials:** copy `.env.example` to `.env` and fill in the tenant ID,
   client (application) ID, and client secret from the app registration.
   That secret is a tenant-wide credential: with the permissions above,
   anyone who copies `.env` can create accounts, change group membership
   and — with the optional password permission — reset non-admin passwords
   from anywhere, with no MFA in the way. Treat it like a domain-admin
   password: give it the shortest lifetime the admin center offers and put
   the expiry in your calendar (the sandbox app in entra-terraform rotates
   its secret every 180 days), keep `.env` out of OneDrive and other synced
   folders and readable only by your own account, and replace the secret at
   once if the machine is lost. Conditional Access for workload identities
   (a separate add-on license) can additionally pin the app to the office
   network.

4. **Config:** copy `config.example.yaml` to `config.yaml` and fill in the
   tenant domain, the role-account prefixes used at your properties, the
   license to assign — a flat `license_sku`, or rule-based `licensing`
   chains (`python provision.py skus` lists the IDs; leave both out in a
   tenant with no licenses and the step is skipped) — and the property
   number → group ID mappings.

Optional permissions unlock extras:

- `User-PasswordProfile.ReadWrite.All` **plus the User Administrator
  directory role** — required for the password reset in `reuse`. App-only
  password changes aren't covered by `User.ReadWrite.All`, and Microsoft's
  `user: update` reference adds that in app-only scenarios the calling app
  must also be assigned at least the User Administrator role: Entra admin
  center → Roles and administrators → User Administrator → Add assignment →
  pick the app's service principal. That role resets non-admin accounts
  only; an account holding an admin role needs Privileged Authentication
  Administrator, which the tool deliberately doesn't ask for. Without
  either piece, `reuse` stops cleanly before changing anything.
- `AuditLog.Read.All` (plus an Entra ID P1 license) — lets `discover` and
  the `reuse` preview show when an account was last used: the most recent
  of its interactive, non-interactive (a phone's mail app refreshing a
  token) and last-successful sign-ins, labelled with which it was, so a
  role account in daily use on a phone doesn't look abandoned. Without the
  permission the column is skipped.
- `LicenseAssignment.Read.All` — lets `skus` list the tenant's license SKUs
  and their IDs, and lets rule-based `licensing` check free seats live (the
  reference's least-privileged permission for `subscribedSkus`;
  `Organization.Read.All` also works but reads far more).
- `UserAuthenticationMethod.ReadWrite.All` — lets `reuse` remove the
  previous holder's registered MFA methods (phone, Authenticator, security
  keys, passkeys, QR-code PIN, external MFA) so the new hire enrolls fresh;
  without it the run still completes but reports the un-wiped methods as an
  issue and exits 1, since the old holder's phone would otherwise answer the
  new hire's MFA prompts.
- **Delegated** `Mail.ReadWrite` plus "Allow public client flows" (on the
  app registration's Authentication page) — unlocks `--open-draft` and
  `capture-signature`, which sign in as the operator (device-code prompt)
  and touch only that one mailbox. The refresh token from that sign-in is
  cached outside the repo, under your own profile
  (`%LOCALAPPDATA%\employee-provisioning-tool\token_cache.json` on Windows,
  `~/.config/employee-provisioning-tool/` elsewhere) with owner-only
  permissions; `python provision.py sign-out` deletes it.
- Not a permission, but a prerequisite: `--join-dls` and `--convert-shared`
  shell out to Exchange Online PowerShell, so they need
  `Install-Module ExchangeOnlineManagement` and an Exchange admin or
  recipient management role on the operator's own account.

## Usage

With the venv activated:

```
python provision.py list-users            # all users — the auth smoke test
python provision.py discover 619          # role accounts for property 619
python provision.py discover manager619   # accounts matching a UPN prefix
python provision.py new                   # fresh account for the hire in hire.yaml
python provision.py new --upn tsmith2     # ...with an explicit UPN
python provision.py new --join-dls        # ...also joining distribution lists (signs in as you)
python provision.py new --open-draft      # ...also drafting the email in your Outlook Drafts
python provision.py reuse                 # preview handing reuse_upn's account to the hire
python provision.py reuse --yes           # actually hand it over
python provision.py reuse --upn manager619 --yes
python provision.py skus                  # license SKU IDs for config.yaml
python provision.py capture-signature     # one-time: save your Outlook signature for --open-draft
python provision.py sign-out              # forget the cached delegated sign-in
python provision.py terminate manager619        # preview the offboarding plan
python provision.py terminate manager619 --yes  # actually offboard
python provision.py terminate manager619 --yes --convert-shared  # ...keeping mail in a shared mailbox
python provision.py new --dry-run               # rehearse any write command
```

`new`, `reuse`, and `terminate` all take `--dry-run`: reads still hit the API
so the output is realistic (real group names, real collision checks), but
every write becomes a `[dry-run] would ...` line — the clipboard copy and,
with `--open-draft`, the Outlook draft included, so a rehearsal never signs
you in or leaves a draft behind. Every action — real or
dry-run — is appended to `logs/provision-<date>.log` with a timestamp;
passwords and personal contact details never go in the log.

`new` builds the UPN from first initial + last name; if that's taken it
automatically tries two letters of the first name, then three, and so on
(numbered variants as a last resort), reporting each taken address and who
holds it. "Taken" covers more than existing UPNs: an address that any user
or group already receives mail at — as its primary address, as an alias
(`proxyAddresses`, including the personal aliases the tool itself suggests
for role accounts) or as a mail nickname — is skipped too, so a new
account's mail can't land in someone else's mailbox. An explicit `--upn` is never substituted — if it's taken, the run
stops. The account is created with a temporary must-change password. If a
`new` run is interrupted after the account exists (a network drop between
the create and the license step, say), **don't run `new` again** — the UPN
ladder would step past the half-made account and create a second one. `new`
guards against this by stopping when an account with the hire's exact name
was created in the last week; finish the hire with `reuse --upn <that upn>
--yes --force` instead, which resets the password, assigns the license and
groups, and prints the email draft exactly as `new` would have. `reuse` locks the departed
employee out first — password reset, then session revocation — then wipes
their registered MFA methods so the new hire enrolls their own, before
renaming and re-enabling the account. Like `terminate`, it changes nothing
without `--yes`: on its own it prints the account it would take over (whose
name is on it, enabled or disabled, last sign-in) and the plan. Because a
mistyped UPN would lock a working employee out, it also refuses — unless
`--force` is given — when the target is still enabled, when its UPN isn't a
configured role prefix (`naming.roles`), or when the role account belongs to
a different property than `hire.yaml` names. Because `reuse` only ever adds
memberships, it ends by listing every group the account still holds that
`config.yaml` doesn't map to the hire's title or property — memberships
inherited from the previous holder, kept for you to review — and reports
any inherited directory role as an issue.

Either path stamps the hire's details onto the account's contact fields:
title → Job title, property name → Office, property number → Department —
so the admin center shows at a glance which property an account belongs to.
Properties run as one can be entered as the pair the form writes
(`property_number: "720/721"`): the account is stamped with Department
`720/721`, joins both properties' groups, CCs every distinct RPM, and takes
its display name from `joined_properties` in `config.yaml` (or the two names
joined with "&" when there's no entry). `discover 720/721` checks the role
prefixes at both numbers.
Display names follow the role-account convention: accounts at a property
display as "{title} at {property name}" (e.g. "Property Manager at Example
Apartments"), while accounts at the corporate office (`corporate_property`
in `config.yaml`) keep a personal "First Last" name. When a form words a
title awkwardly, `naming.title_display` can restate it for the display name
alone ("Concierge/Leasing" → "Leasing Concierge") — the Job title attribute
and every matching rule keep the form's exact wording. The login-info email
always addresses the person by name either way.
The tool then assigns the configured license — either the flat `license_sku`,
or rule-based `licensing` chains keyed by who the hire is (corporate property,
maintenance title, or everyone else), where the first SKU with free seats
wins, seat counts checked live via `LicenseAssignment.Read.All`. With neither
configured the step is skipped with a note. On `reuse`, a role account that
still carries a license from its chain keeps it and nothing is assigned; one
carrying a license from outside its chain is reported as an issue rather
than given a second paid seat. It then joins the account to every group the hire
qualifies for, merged from three sources in `config.yaml`: the property's
own groups, a corporate-or-site set (chosen by comparing the property
number to `corporate_property`), and job-title groups (case-insensitive
match under `groups.titles`). Each join is reported by group name, an
already-present membership counts as fine, and duplicates across sources
collapse. Transient Graph throttling and
concurrency errors are retried automatically — except that the account
creation itself is never resent after a gateway timeout, since the first
attempt may have gone through; the run stops and says so instead.

Classic Exchange distribution lists are the exception: their membership is
read-only through the Graph API. By default the run prints their joins as
paste-ready `Add-DistributionGroupMember` commands, addressed by each list's
SMTP address — run `Connect-ExchangeOnline` once and keep the window open,
and each hire's joins become a two-second paste (the admin center's Assign
memberships panel works too). Or opt in with `--join-dls` (on `new` or
`reuse`) and the tool opens the Exchange Online PowerShell session itself —
like `--convert-shared`, that one step runs as *your* signed-in account,
needs the `ExchangeOnlineManagement` module plus an Exchange admin or
recipient management role, and treats already-a-member as success. Either
way, a just-created account can take a minute to become visible to Exchange —
if a join fails on that, retry it a minute later.

`terminate` offboards in lockout-first order: disable the account and revoke
every session, then remove group memberships and licenses. Memberships are
handled by kind: ordinary groups are left via Graph, distribution lists
print as paste-ready `Remove-DistributionGroupMember` commands (Graph can't
touch them), and dynamic groups are noted and skipped since their
membership follows attributes. With `--convert-shared`, **every membership
is kept** — a shared mailbox usually exists so that group and list mail
keeps arriving. What it covers is exactly that: the account, its
Graph-managed group memberships and its licenses. Anything it can't finish
itself is reported as an open follow-up and the run exits 1 until it's done:
the printed distribution-list removals, and any **directory role** the
account holds (removing roles would need `RoleManagement.ReadWrite.Directory`,
which the app deliberately doesn't carry, so they're handed to the admin
center's Roles and administrators page). Registered devices, app ownership,
OneDrive hand-off and mail forwarding are outside the tool. Without
`--yes` it only prints who would be offboarded and what would happen — the
destructive path always requires the flag. Converting the mailbox to shared
(if mail must be retained) is printed as a manual follow-up by default, since
mailbox type is an Exchange setting outside the Graph v1.0 API. Opt in with
`--convert-shared` and the tool does it for you between lockout and license
removal — by shelling out to Exchange Online PowerShell (`Set-Mailbox -Type
Shared`, then turning on both "Manage sent items" copies so mail sent as or
on behalf of the mailbox lands in its own Sent Items), the one step that
runs as *your* signed-in account rather than the app registration. It needs the `ExchangeOnlineManagement` module
(`Install-Module ExchangeOnlineManagement`) and an Exchange admin role, and
`Connect-ExchangeOnline` prompts for sign-in mid-run. With PowerShell 7
installed that's a device code printed in the terminal; with Windows
PowerShell 5.1 the session opens in a console window of its own, because
its Windows account-broker sign-in needs a real window to attach to (an
editor's terminal hangs it, and the module's browser fallback is a legacy
control that sign-in pages reject). If an automatic join fails for any
reason, the paste-ready commands print anyway so the hire can be finished
by hand. If the conversion
fails, the licenses are deliberately left in place — removing a license from
an unconverted mailbox starts its deletion clock.

When the account's UPN is role-format (`{role}{property number}@`, e.g.
`manager536@`), the tool also picks the first free personal address in the
`{first initial}{last name}` convention and prints it as a manual
add-an-alias step — the Graph API can't write Exchange aliases
(`proxyAddresses` is read-only), so that last touch happens in the admin
center.

After provisioning, the tool prints the manual checklist of non-M365
platforms marked on the form and a ready-to-paste login-info email (CC'ing
the RPM when the form asks). The tool prints the temporary password once and
writes it to no file of its own, but the credential does travel with the
draft: the body — password included — lands on the clipboard as rich text so
a paste into Outlook keeps the login link clickable (Windows clipboard
history or a synced clipboard keeps that copy until it's cleared or
overwritten), and with `--open-draft` it sits in the draft, and later in
Sent Items, like any other email. With `--open-draft` (on `new` or `reuse`), the email is
created directly in **your Outlook Drafts folder** via the Graph API —
recipients, subject, formatted body, any `email_attachments` from
`config.yaml` (say, MFA setup instructions — files of 3 MB and up go
through an upload session, up to Outlook's 150 MB limit), and your captured
signature already in place. It appears in new Outlook, the web, and your
phone like any other draft; nothing is sent until you open it and click
Send, and deleting it discards it. If the draft can't be created, the
printed draft and clipboard copy still stand; if it is created but an
attachment or signature image fails to upload, the run says "draft created
but incomplete" and names what to add in Outlook.

This is the tool's one delegated feature: it signs in as *you* (a
device-code prompt, cached so it's occasional) and touches only your own
mailbox — the app registration needs the **delegated** `Mail.ReadWrite`
permission and "Allow public client flows" enabled, never the tenant-wide
application version. Because Outlook only inserts signatures into mail
composed in the client (and offers no API to read them), `capture-signature`
copies yours once: compose an empty email in Outlook (the signature inserts
itself), subject it `signature-capture`, save it as a draft, and run the
command — the signature's HTML and inline images are stored locally
(git-ignored) and appended to every generated draft from then on. Re-run it
whenever your signature changes.

The email wording is yours to edit: copy `email_template.example.txt` to
`email_template.txt` (git-ignored, so it can carry company-specific text)
and write what you like, using the placeholders `{name}`, `{first}`,
`{last}`, `{username}`, and `{password}`. The first line is the subject (a
leading `Subject:` label, as in the example, is optional and stripped); end
the body at your sign-off and let the captured signature carry the rest.

`hire.yaml` (git-ignored) carries the current hire's details, copied from
the Formstack ticket's fields — about 30 seconds of copying that replaces
the whole portal walk-through (until Phase 6 pulls them automatically):

```yaml
# Quote the text values: YAML would otherwise read a surname like No as
# false, or a number like 050 as 40.
first_name: "Taylor"
last_name: "Example"
title: "Property Manager"
property_number: "619"        # or a joined pair as the form writes it: "720/721"
# property_name is optional when config.yaml's property list has the number —
# the tool fills it in from there (an explicit value here still wins)
property_name: "Example Apartments"
# reuse mode only — the role account being handed over:
reuse_upn: "manager619"

# where the login info goes, and whether to CC the RPM — leave rpm_email
# blank and it fills in from the property's rpm: in config.yaml
login_info_email: manager619@example.com
copy_rpm: yes
rpm_email: ""

# non-M365 platforms marked on the form — printed as a manual checklist
platforms:
  yardi: yes
  happyco: no
  rent_cafe: yes
```

## Testing

The test suite exercises the tool's decision logic against in-memory fakes,
so it needs no credentials, no `.env`, and no tenant, and makes zero network
calls — so it's safe to run on any machine, including one whose `.env` points
at production. It covers three areas:

- **Offboarding** (`tests/test_terminate.py`): a recording fake of the Graph
  client asserts the *order* of operations — disable and revoke sessions
  first, then memberships, then licenses. It also checks that a preview or
  `--dry-run` writes nothing, that only Graph-managed groups are removed
  (distribution lists come back as paste-ready commands, dynamic groups are
  left alone), that one failed group removal doesn't stop the rest, and that
  a failed `--convert-shared` leaves the licenses in place.
- **The Graph client** (`tests/test_graph_client.py`): a scripted fake of
  `requests.Session` covers token caching and early refresh, `Retry-After`
  and backoff on 429/503/504, the directory-concurrency retry, giving up
  after three attempts, error-message extraction, and `@odata.nextLink`
  paging.
- **Distribution list joins** (`tests/test_distribution_lists.py`): a fake
  of the Exchange session checks the PowerShell the tool generates — every
  value stays inside its single-quoted string even with an apostrophe in a
  list address — plus the joined, failed, and session-never-ran outcomes.
- **UPN generation** (`tests/test_upn_generation.py`): the collision-safe
  username ladder.

From the repo root, with the venv activated:

```
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Plain `pytest -q` works too (`pyproject.toml` puts the repo root on the
import path, which is what an IDE's test runner uses). Without activating,
call the venv's interpreter directly:
`venv/bin/python -m pytest -q` on macOS/Linux, `venv\Scripts\python -m pytest -q`
on Windows. GitHub Actions runs the same command on every push and pull
request (`.github/workflows/tests.yml`, Python 3.12).
