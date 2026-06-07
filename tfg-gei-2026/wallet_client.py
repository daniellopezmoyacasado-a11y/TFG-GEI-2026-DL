import os
import requests
from dataclasses import dataclass
from typing import Any, Dict, Optional

from dotenv import load_dotenv
load_dotenv()

#This file is reused across services, it's meant to use the already existing account logic of the wallet to avoid reduntant development,
#allowing the project to focus on the demonstration of the Verifiable Credential technology.


class WaltIdAuthError(Exception):
    """Raised when credentials are invalid or the wallet rejects the login."""


class WaltIdUnexpectedError(Exception):
    """Raised for unexpected wallet responses (schema changes, server errors, etc.)."""


@dataclass
class WaltIdConfig:
   
    base_url: str = os.getenv("WALTID_WALLET_URL", "https://wallet.localhost")
    timeout_s: int = int(os.getenv("WALTID_TIMEOUT_S", "30"))
    login_type: str = os.getenv("WALTID_LOGIN_TYPE", "email")


class WaltIdClient:
    """
    Minimal walt.id Wallet API client for:
      - registering users
      - login with email + passwords
      - token, wallet userid and email/username
      - logout

    It is designed to resemble the current KratosClient usage style.
    """

    def __init__(self, config: Optional[WaltIdConfig] = None):
        self.cfg = config or WaltIdConfig()

    def _url(self, path: str) -> str:
        base = self.cfg.base_url.rstrip("/")
        return f"{base}{path}"

    def _auth_path(self, suffix: str) -> str:
        """
        Supports both types of urls, given different walt.id versions
        """
        base = self.cfg.base_url.rstrip("/")
        if base.endswith("/wallet-api"):
            return self._url(f"/auth/{suffix.lstrip('/')}")
        return self._url(f"/wallet-api/auth/{suffix.lstrip('/')}")

    def register_user(self, name: str, email: str, password: str) -> Dict[str, Any]:
        payload = {
            "name": name,
            "email": email,
            "password": password,
            "type": self.cfg.login_type,
        }

        r = requests.post(
            self._auth_path("register"),
            json=payload,
            timeout=self.cfg.timeout_s,
        )
        
        if r.status_code in (400, 401, 403, 409):
            raise WaltIdAuthError(f"Registration rejected: {r.status_code} {r.text}")

        if r.status_code not in (200, 201):
            raise WaltIdUnexpectedError(
                f"Register failed: {r.status_code} {r.text}"
            )
        #Some wallet versions return JSON, others plain text 
        content_type = r.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            try:
                return r.json()
            except Exception as e:
                raise WaltIdUnexpectedError(
                    f"Register response was not valid JSON: {r.text}"
                ) from e
        return {
        "success": True,
        "message": r.text.strip() or "Registration succeeded"
    }

    def login(self, email: str, password: str) -> Dict[str, Any]:
        payload = {
            "type": self.cfg.login_type,
            "email": email,
            "password": password,
        }

        r = requests.post(
            self._auth_path("login"),
            json=payload,
            timeout=self.cfg.timeout_s,
        )

        if r.status_code in (400, 401, 403):
            raise WaltIdAuthError(f"Invalid credentials or login rejected: {r.text}")

        if r.status_code != 200:
            raise WaltIdUnexpectedError(
                f"Login failed: {r.status_code} {r.text}"
            )

        try:
            data = r.json()
        except Exception as e:
            raise WaltIdUnexpectedError(
                f"Login response was not valid JSON: {r.text}"
            ) from e

        token = data.get("token")
        wallet_user_id = data.get("id")
        username = data.get("username")

        if not token:
            raise WaltIdUnexpectedError(f"Login response missing token: {data}")
#username fallback to the email used for login
        if not username:
            username = email

        if not wallet_user_id:
            raise WaltIdUnexpectedError(f"Login response missing id: {data}")

        return {
            "token": token,
            "user_id": wallet_user_id,
            "email": username,
            "raw": data,
        }

    def logout(self, token: str) -> None:
        r = requests.post(
            self._auth_path("logout"),
            headers=self.auth_headers(token),
            timeout=self.cfg.timeout_s,
        )

        if r.status_code not in (200, 204):
            raise WaltIdUnexpectedError(
                f"Logout failed: {r.status_code} {r.text}"
            )

    def auth_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}"
        }

    def verify_and_get_user_id(self, email: str, password: str) -> str:
        login_data = self.login(email, password)
        token = login_data["token"]
        try:
            return login_data["user_id"]
        finally:
            try:
                self.logout(token)
            except Exception:
                pass

    def verify_and_get_email(self, email: str, password: str) -> str:
        login_data = self.login(email, password)
        token = login_data["token"]
        try:
            return login_data["email"]
        finally:
            try:
                self.logout(token)
            except Exception:
                pass

    def verify_and_get_user(self, email: str, password: str) -> Dict[str, str]:
        """
        Returns both the wallet user id and the email after successful login.
        """
        login_data = self.login(email, password)
        token = login_data["token"]
        try:
            return {
                "user_id": login_data["user_id"],
                "email": login_data["email"],
            }
        finally:
            try:
                self.logout(token)
            except Exception:
                pass
