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
  tenant-specific IDs in a git-ignored `config.yaml` (a committed
  `config.example.yaml` will document the shape). The `.gitignore` was the
  repository's first commit.
- **Personal data stays out of the repo and the logs:** per-hire input files
  are git-ignored, and the audit log records actions taken, not personal
  details.
- **Reuse or create — human decides:** some hires take over an existing role
  account, others get a fresh personal one. A discovery step reports the role
  account's status (name, enabled/disabled, last activity); the operator
  chooses `--reuse` or `--new`, and the tool does the clicking either way.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Client-credentials auth, list users | **done** |
| 2 | Discovery + create (`--new`) or reuse (`--reuse`) with session revocation | planned |
| 3 | License assignment + property/role group membership | planned |
| 4 | Offboarding: disable, strip groups and license | planned |
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

## Usage

With the venv activated:

```
python provision.py list-users
```

Lists every user in the tenant with UPN, enabled/disabled state, and title —
the smoke test that proves auth works before anything that writes.
