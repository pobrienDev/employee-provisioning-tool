"""Authentication and Microsoft Graph API calls.

All HTTP against Microsoft Graph lives in this module. provision.py imports
GraphClient and never talks to the network directly.

Auth is the OAuth 2.0 client-credentials flow (app-only): the tool
authenticates as an Entra ID app registration using the tenant ID, client ID,
and client secret from the environment — it never uses a signed-in user's
session, so it can only ever touch the tenant configured in .env.
"""

import os
import time
from urllib.parse import quote

import requests
from dotenv import load_dotenv

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


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

        Values come from the process environment, with .env loaded first
        if one exists next to the script.
        """
        load_dotenv()
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

    def _request(self, method, path, **kwargs):
        """Send an authenticated request to Graph and return the response.

        `path` is either a path under the v1.0 base ("/users") or a full URL
        (as returned in @odata.nextLink paging links).
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
            if attempt < 2 and self._transient(response):
                try:
                    delay = int(response.headers.get("Retry-After", ""))
                except ValueError:
                    delay = 2 * (attempt + 1)
                time.sleep(max(delay, 1))
                continue
            break
        if response.status_code >= 400:
            raise GraphError(self._error_message(response), status=response.status_code)
        return response

    @staticmethod
    def _transient(response):
        """True for throttling/outage responses Graph tells clients to retry."""
        if response.status_code in (429, 503, 504):
            return True
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
        """Create a user and return the new account object."""
        return self._request("POST", "/users", json=payload).json()

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
