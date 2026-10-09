"""Authentication and Microsoft Graph API calls.

All HTTP against Microsoft Graph lives in this module. provision.py imports
GraphClient and never talks to the network directly.

Auth is the OAuth 2.0 client-credentials flow (app-only): the tool
authenticates as an Entra ID app registration using the tenant ID, client ID,
and client secret from the environment, so it can only ever touch the tenant
configured in .env. The one exception is DelegatedGraphClient, used solely by
the mailbox features (drafts, signature capture): it signs the operator in
and reaches that operator's own mailbox and nothing else.
"""

import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
DEVICE_CODE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"

# Delegated scope for the mailbox features (drafts, signature capture):
# Mail.ReadWrite as the signed-in user reaches that one mailbox and nothing
# else. offline_access adds the refresh token that keeps sign-ins occasional.
DELEGATED_SCOPE = "https://graph.microsoft.com/Mail.ReadWrite offline_access"


def _token_cache_dir():
    """Per-user, non-synced application data: %LOCALAPPDATA% on Windows,
    ~/.config elsewhere. Kept out of the repo folder, which may be backed
    up, synced, or readable by other local users."""
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".config")
    return Path(base) / "employee-provisioning-tool"


# The delegated refresh token. Anyone holding it can read and write the
# operator's mailbox until it expires or is revoked, so it lives under the
# user profile with owner-only permissions, not next to the script.
TOKEN_CACHE = _token_cache_dir() / "token_cache.json"
# Where earlier versions kept it: inside the repo folder. Read once for a
# painless upgrade, then deleted.
LEGACY_TOKEN_CACHE = Path(__file__).parent / ".token_cache.json"


def forget_sign_in():
    """Delete the cached delegated sign-in. Returns the paths removed."""
    removed = []
    for path in (TOKEN_CACHE, LEGACY_TOKEN_CACHE):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed.append(str(path))
    return removed

# Credentials come from the .env next to this module. It is loaded with
# override=True: a TENANT_ID still exported in the shell from some earlier
# session must not quietly redirect a run to a different tenant than the
# file says. Without a .env, the process environment is used as-is.
ENV_FILE = Path(__file__).parent / ".env"

# Registered MFA method types -> the per-type endpoint used to delete them:
# every authenticationMethod type in the Graph v1.0 reference except
# passwordAuthenticationMethod, which can't be deleted, only reset.
AUTH_METHOD_PATHS = {
    "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod": "microsoftAuthenticatorMethods",
    "#microsoft.graph.phoneAuthenticationMethod": "phoneMethods",
    "#microsoft.graph.fido2AuthenticationMethod": "fido2Methods",
    "#microsoft.graph.emailAuthenticationMethod": "emailMethods",
    "#microsoft.graph.softwareOathAuthenticationMethod": "softwareOathMethods",
    "#microsoft.graph.windowsHelloForBusinessAuthenticationMethod": "windowsHelloForBusinessMethods",
    "#microsoft.graph.temporaryAccessPassAuthenticationMethod": "temporaryAccessPassMethods",
    "#microsoft.graph.platformCredentialAuthenticationMethod": "platformCredentialMethods",
    "#microsoft.graph.externalAuthenticationMethod": "externalAuthenticationMethods",
    "#microsoft.graph.qrCodePinAuthenticationMethod": "qrCodePinMethod",
}
# A user has at most one of these, so the endpoint takes no method ID.
SINGLETON_AUTH_METHOD_PATHS = {"qrCodePinMethod"}
PASSWORD_METHOD_TYPE = "#microsoft.graph.passwordAuthenticationMethod"


# Outlook attachment limits: a single POST takes files under 3 MB; larger
# files go through an upload session in ranges of at most 4 MB, up to 150 MB.
ATTACHMENT_INLINE_LIMIT = 3 * 1024 * 1024
ATTACHMENT_UPLOAD_LIMIT = 150 * 1024 * 1024
UPLOAD_CHUNK = 3 * 1024 * 1024


class ConfigError(Exception):
    """Required environment configuration is missing."""


class GraphError(Exception):
    """A Graph API call failed; the message carries the API's own error."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GraphClient:
    """App-only Microsoft Graph client with token caching."""

    def __init__(self, tenant_id, client_id, client_secret):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = requests.Session()
        self._token = None
        self._token_expires = 0.0

    @classmethod
    def from_env(cls):
        """Build a client from TENANT_ID, CLIENT_ID, and CLIENT_SECRET.

        Values come from the .env next to this module when it exists (and
        win over anything already in the process environment), otherwise
        from the process environment.
        """
        load_dotenv(ENV_FILE, override=True)
        required = ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ConfigError(
                f"missing {', '.join(missing)} — copy .env.example to .env and "
                "fill in the app registration values for your test tenant"
            )
        return cls(*(os.environ[name] for name in required))

    def _get_token(self):
        """Return a valid access token, requesting a new one when needed."""
        if self._token and time.time() < self._token_expires:
            return self._token

        try:
            response = self.session.post(
                TOKEN_URL.format(tenant=self.tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise GraphError(f"cannot reach login.microsoftonline.com: {exc}") from exc

        if response.status_code != 200:
            raise GraphError(
                f"token request failed ({response.status_code}): {response.text}",
                status=response.status_code,
            )

        payload = response.json()
        self._token = payload["access_token"]
        # Refresh a minute early so a token can't expire mid-request.
        self._token_expires = time.time() + int(payload.get("expires_in", 3600)) - 60
        return self._token

    def _request(self, method, path, idempotent=True, **kwargs):
        """Send an authenticated request to Graph and return the response.

        `path` is either a path under the v1.0 base ("/users") or a full URL
        (as returned in @odata.nextLink paging links).

        Throttling and outage responses are retried, except that a request
        which is not idempotent (creating a user, creating a draft) is never
        resent after a 503/504: the gateway may have timed out *after* the
        directory applied the create, and a blind retry would then fail on
        "already exists" with no sign that the first attempt went through.
        """
        url = path if path.startswith("https://") else f"{GRAPH_BASE}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._get_token()}"
        for attempt in range(3):
            try:
                response = self.session.request(
                    method, url, headers=headers, timeout=30, **kwargs
                )
            except requests.RequestException as exc:
                raise GraphError(f"{method} {url} failed: {exc}") from exc
            if attempt < 2 and self._transient(response, idempotent):
                try:
                    delay = int(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2 * (attempt + 1)
                time.sleep(max(delay, 1))
                continue
            break
        if response.status_code >= 400:
            message = self._error_message(response)
            if not idempotent and response.status_code in (503, 504):
                message += (
                    " — not retried: the request may already have been applied, "
                    "so check before running it again"
                )
            raise GraphError(message, status=response.status_code)
        return response

    @staticmethod
    def _transient(response, idempotent=True):
        """True for throttling/outage responses Graph tells clients to retry.

        A 429 means the request was not processed, and a concurrency
        conflict means the write was rejected, so both are always safe to
        resend. A 503/504 is ambiguous — the request may have gone through
        — so only idempotent requests retry on those.
        """
        if response.status_code == 429:
            return True
        if response.status_code in (503, 504):
            return idempotent
        # Rapid writes to the same directory object can collide transiently.
        return (
            response.status_code == 409
            and "Directory_ConcurrencyViolation" in response.text
        )

    @staticmethod
    def _error_message(response):
        """Extract Graph's error code and message from an error response."""
        try:
            error = response.json()["error"]
            detail = f"{error['code']}: {error['message']}"
        except (ValueError, KeyError, TypeError):
            detail = response.text
        return f"Graph API error ({response.status_code}) — {detail}"

    def create_draft(self, payload):
        """Create a draft message in the signed-in mailbox (delegated only)."""
        return self._request("POST", "/me/messages", idempotent=False, json=payload).json()

    def add_attachment(self, message_id, payload):
        """Attach a file under 3 MB to a draft message (delegated only)."""
        self._request(
            "POST", f"/me/messages/{quote(message_id, safe='')}/attachments",
            json=payload,
        )

    def add_file_attachment(self, message_id, name, data, content_type=None,
                            content_id=None, is_inline=False):
        """Attach a file to a draft message, whatever its size (delegated only).

        Under 3 MB it is one POST with the content inline. From 3 MB the
        single POST is refused, so the file goes through an upload session:
        createUploadSession returns a pre-authenticated URL (no Authorization
        header on it), and the bytes are PUT in order in ranges under 4 MB.
        """
        if len(data) < ATTACHMENT_INLINE_LIMIT:
            payload = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentBytes": base64.b64encode(data).decode("ascii"),
            }
            if content_type:
                payload["contentType"] = content_type
            if content_id:
                payload["contentId"] = content_id
                payload["isInline"] = bool(is_inline)
            self.add_attachment(message_id, payload)
            return

        item = {"attachmentType": "file", "name": name, "size": len(data)}
        if content_type:
            item["contentType"] = content_type
        if content_id:
            item["contentId"] = content_id
            item["isInline"] = bool(is_inline)
        session = self._request(
            "POST",
            f"/me/messages/{quote(message_id, safe='')}/attachments/createUploadSession",
            idempotent=False, json={"AttachmentItem": item},
        ).json()
        upload_url = session["uploadUrl"]
        total = len(data)
        for start in range(0, total, UPLOAD_CHUNK):
            chunk = data[start:start + UPLOAD_CHUNK]
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}",
            }
            try:
                response = self.session.put(upload_url, data=chunk, headers=headers, timeout=120)
            except requests.RequestException as exc:
                raise GraphError(f"upload of {name} failed: {exc}") from exc
            if response.status_code >= 400:
                raise GraphError(
                    f"upload of {name} failed ({response.status_code}): {response.text}",
                    status=response.status_code,
                )

    def find_drafts(self, subject, select="id,subject,lastModifiedDateTime"):
        """Drafts whose subject matches exactly (delegated only)."""
        escaped = subject.replace("'", "''")
        params = {"$filter": f"subject eq '{escaped}'", "$select": select}
        return self._request(
            "GET", "/me/mailFolders/drafts/messages", params=params
        ).json().get("value", [])

    def get_message(self, message_id, select="id,subject,body"):
        """One message from the signed-in mailbox (delegated only)."""
        path = f"/me/messages/{quote(message_id, safe='')}?$select={select}"
        return self._request("GET", path).json()

    def get_attachments(self, message_id):
        """A message's attachments, content included (delegated only)."""
        path = f"/me/messages/{quote(message_id, safe='')}/attachments"
        return self._request("GET", path).json().get("value", [])

    def list_users(self):
        """Return all users in the tenant, following paging links."""
        users = []
        url = "/users?$select=displayName,userPrincipalName,jobTitle,accountEnabled"
        while url:
            page = self._request("GET", url).json()
            users.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
        return users

    def find_users(self, upn_prefix, select):
        """Return users whose userPrincipalName starts with the prefix."""
        # OData string literals escape single quotes by doubling them.
        escaped = upn_prefix.replace("'", "''")
        params = {
            "$filter": f"startswith(userPrincipalName,'{escaped}')",
            "$select": select,
        }
        return self._request("GET", "/users", params=params).json()["value"]

    def address_holder(self, local, domain):
        """Who already receives mail at local@domain, or None.

        A UPN lookup matches UPNs and object IDs only. Exchange also
        delivers to every alias in proxyAddresses, to a group's address,
        and routes on mailNickname — and this tool itself tells operators
        to add personal aliases to role accounts. So before a UPN is
        called free, users and groups are checked for the address in
        mail or proxyAddresses (either case of the smtp: prefix) and for a
        matching mailNickname. Returns a label naming the holder.
        """
        address = f"{local}@{domain}".replace("'", "''")
        nickname = local.replace("'", "''")
        clauses = (
            f"mail eq '{address}' or mailNickname eq '{nickname}' "
            f"or proxyAddresses/any(p:p eq 'smtp:{address}') "
            f"or proxyAddresses/any(p:p eq 'SMTP:{address}')"
        )
        for kind, path, select in (
            ("user", "/users", "displayName,userPrincipalName"),
            ("group", "/groups", "displayName,mail"),
        ):
            params = {"$filter": clauses, "$select": select, "$top": "1"}
            found = self._request("GET", path, params=params).json().get("value", [])
            if found:
                item = found[0]
                where = item.get("userPrincipalName") or item.get("mail") or kind
                return f"{item.get('displayName') or kind}, {where}"
        return None

    def find_users_by_name(self, given_name, surname, select):
        """Users whose givenName and surname both match exactly."""
        given = given_name.replace("'", "''")
        family = surname.replace("'", "''")
        params = {
            "$filter": f"givenName eq '{given}' and surname eq '{family}'",
            "$select": select,
        }
        return self._request("GET", "/users", params=params).json()["value"]

    def get_user(self, upn_or_id, select):
        """Return one user, or None if no such account exists."""
        try:
            path = f"/users/{quote(upn_or_id, safe='')}?$select={select}"
            return self._request("GET", path).json()
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    def create_user(self, payload):
        """Create a user and return the new account object.

        Not retried on 503/504 (see _request): a second POST after a create
        that actually went through would leave an account whose temporary
        password was never shown to anyone.
        """
        return self._request("POST", "/users", idempotent=False, json=payload).json()

    def update_user(self, user_id, changes):
        """PATCH attributes on an existing user."""
        self._request("PATCH", f"/users/{user_id}", json=changes)

    def revoke_sessions(self, user_id):
        """Invalidate the user's sign-in sessions and refresh tokens."""
        self._request("POST", f"/users/{user_id}/revokeSignInSessions")

    def assign_license(self, user_id, sku_id):
        """Assign a license SKU to the user (account needs a usageLocation)."""
        payload = {"addLicenses": [{"skuId": sku_id}], "removeLicenses": []}
        self._request("POST", f"/users/{user_id}/assignLicense", json=payload)

    def list_skus(self):
        """Return the tenant's subscribed license SKUs."""
        return self._request("GET", "/subscribedSkus").json().get("value", [])

    def get_group(self, group_id, select="displayName"):
        """Return a group, or None if no such group exists."""
        try:
            return self._request("GET", f"/groups/{group_id}?$select={select}").json()
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise

    def add_group_member(self, group_id, user_id):
        """Add the user to a group."""
        payload = {"@odata.id": f"{GRAPH_BASE}/directoryObjects/{user_id}"}
        self._request("POST", f"/groups/{group_id}/members/$ref", json=payload)

    def remove_group_member(self, group_id, user_id):
        """Remove the user from a group."""
        self._request("DELETE", f"/groups/{group_id}/members/{user_id}/$ref")

    def _member_of(self, user_id, select, odata_type):
        """The user's direct memberships of one directoryObject type.

        memberOf returns groups, directory roles and administrative units
        together; callers ask for the kind they can act on.
        """
        found = []
        url = f"/users/{user_id}/memberOf?$select={select}"
        while url:
            page = self._request("GET", url).json()
            found.extend(
                item for item in page.get("value", [])
                if item.get("@odata.type") == odata_type
            )
            url = page.get("@odata.nextLink")
        return found

    def get_member_groups(self, user_id):
        """Return the groups the user belongs to, with enough of each
        group's shape (types, mail settings) to tell how to leave it."""
        return self._member_of(
            user_id,
            "id,displayName,groupTypes,mailEnabled,securityEnabled,mail",
            "#microsoft.graph.group",
        )

    def get_member_roles(self, user_id):
        """Return the directory roles the user holds (id, displayName).

        A role is a different kind of privilege than a group membership and
        removing one needs RoleManagement.ReadWrite.Directory, which this
        tool deliberately doesn't hold — so roles are reported, not removed.
        Without a role-reading permission Graph still returns the role, but
        with only its id.
        """
        return self._member_of(user_id, "id,displayName", "#microsoft.graph.directoryRole")

    def remove_licenses(self, user_id, sku_ids):
        """Remove license SKUs from the user."""
        payload = {"addLicenses": [], "removeLicenses": sku_ids}
        self._request("POST", f"/users/{user_id}/assignLicense", json=payload)

    def list_auth_methods(self, user_id):
        """Return the user's registered authentication methods."""
        url = f"/users/{user_id}/authentication/methods"
        return self._request("GET", url).json().get("value", [])

    def delete_auth_method(self, user_id, method_path, method_id):
        """Delete one registered authentication method by its typed endpoint."""
        path = f"/users/{user_id}/authentication/{method_path}"
        if method_path not in SINGLETON_AUTH_METHOD_PATHS:
            path += f"/{method_id}"
        self._request("DELETE", path)


class DelegatedGraphClient(GraphClient):
    """Graph client acting as the signed-in operator, not the app.

    Only the mailbox features (Outlook draft creation, signature capture)
    use this: the delegated Mail.ReadWrite scope reaches the signed-in
    user's own mailbox and nothing else, which is why the tool never asks
    for the tenant-wide application version of that permission. Sign-in is
    the device-code flow — a code to enter at microsoft.com/devicelogin —
    and the refresh token is cached under the user profile (TOKEN_CACHE,
    owner-only) so the prompt is occasional rather than per run;
    forget_sign_in() deletes it. Requires the app registration to allow
    public client flows.
    """

    @classmethod
    def from_env(cls):
        """Build a delegated client from TENANT_ID and CLIENT_ID."""
        load_dotenv(ENV_FILE, override=True)
        missing = [name for name in ("TENANT_ID", "CLIENT_ID") if not os.getenv(name)]
        if missing:
            raise ConfigError(
                f"missing {', '.join(missing)} — copy .env.example to .env and "
                "fill in the app registration values"
            )
        return cls(os.environ["TENANT_ID"], os.environ["CLIENT_ID"], None)

    def _get_token(self):
        if self._token and time.time() < self._token_expires:
            return self._token
        refresh = self._cached_refresh_token()
        if refresh and self._redeem({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": self.client_id,
            "scope": DELEGATED_SCOPE,
        }):
            return self._token
        self._device_code_sign_in()
        return self._token

    @staticmethod
    def _cached_refresh_token():
        for path in (TOKEN_CACHE, LEGACY_TOKEN_CACHE):
            try:
                return json.loads(path.read_text(encoding="utf-8")).get("refresh_token")
            except (OSError, ValueError):
                continue
        return None

    def _store(self, payload):
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 3600)) - 60
        if payload.get("refresh_token"):
            try:
                TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
                # Create (or truncate) owner-only before any token lands in it.
                fd = os.open(TOKEN_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"refresh_token": payload["refresh_token"]}))
                os.chmod(TOKEN_CACHE, 0o600)
            except OSError:
                pass  # no cache just means the prompt comes back sooner
            try:
                LEGACY_TOKEN_CACHE.unlink()   # a copy in the repo folder is the exposure
            except OSError:
                pass

    def _redeem(self, data):
        """Try a token grant; True on success, False to fall back to sign-in."""
        try:
            response = self.session.post(
                TOKEN_URL.format(tenant=self.tenant_id), data=data, timeout=30
            )
        except requests.RequestException as exc:
            raise GraphError(f"cannot reach login.microsoftonline.com: {exc}") from exc
        payload = response.json()
        if "access_token" not in payload:
            return False
        self._store(payload)
        return True

    def _device_code_sign_in(self):
        try:
            response = self.session.post(
                DEVICE_CODE_URL.format(tenant=self.tenant_id),
                data={"client_id": self.client_id, "scope": DELEGATED_SCOPE},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise GraphError(f"cannot reach login.microsoftonline.com: {exc}") from exc
        if response.status_code != 200:
            raise GraphError(
                f"sign-in could not start ({response.status_code}): {response.text}"
            )
        flow = response.json()
        print(f"\n  {flow['message']}\n")
        interval = int(flow.get("interval", 5))
        deadline = time.time() + int(flow.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            response = self.session.post(
                TOKEN_URL.format(tenant=self.tenant_id),
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": self.client_id,
                    "device_code": flow["device_code"],
                },
                timeout=30,
            )
            payload = response.json()
            if "access_token" in payload:
                self._store(payload)
                return
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            description = payload.get("error_description") or error or "unknown error"
            if "7000218" in description:
                raise GraphError(
                    "sign-in refused — enable 'Allow public client flows' on the "
                    "app registration's Authentication page"
                )
            raise GraphError("sign-in failed — " + description.splitlines()[0])
        raise GraphError("sign-in timed out before the code was entered — run it again")
