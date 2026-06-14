from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from personal_news_agent.config import Settings
from personal_news_agent.services.realname import RealNameVerificationError, RealNameVerificationService
from personal_news_agent.services.store import NewsStore


class AuthError(ValueError):
    pass


class AuthService:
    def __init__(self, store: NewsStore, settings: Settings):
        self.store = store
        self.settings = settings
        self.realname = RealNameVerificationService(settings)

    def register(self, display_name: str, email: str | None, password: str | None) -> dict:
        password_hash = _hash_password(password) if password else None
        if email and self.store.get_user_by_email(email):
            raise AuthError("email already registered")
        user = self.store.create_user(display_name=display_name, email=email, password_hash=password_hash)
        return self._with_session(user)

    def login(self, email: str, password: str) -> dict:
        user = self.store.get_user_by_username(email) or self.store.get_user_by_email(email)
        if not user or not user.get("password_hash") or not _verify_password(password, user["password_hash"]):
            raise AuthError("invalid email or password")
        public_user = {"id": user["id"], "display_name": user["display_name"], "email": user.get("email"), "username": user.get("username"), "mobile": user.get("mobile")}
        return self._with_session(public_user)

    def register_with_realname(self, username: str, password: str, confirm_password: str, real_name: str, mobile: str, id_card: str | None = None) -> dict:
        username = username.strip()
        mobile = mobile.strip()
        if len(username) < 3:
            raise AuthError("username must be at least 3 characters")
        if password != confirm_password:
            raise AuthError("password and confirm_password do not match")
        if self.store.get_user_by_username(username):
            raise AuthError("username already registered")
        if self.store.get_user_by_mobile(mobile):
            raise AuthError("mobile already registered")
        try:
            verification = self.realname.verify(real_name=real_name, id_card=id_card, mobile=mobile)
        except RealNameVerificationError as exc:
            raise AuthError(str(exc)) from exc
        if not verification.passed:
            raise AuthError(verification.message)
        user = self.store.create_verified_user(
            username=username,
            password_hash=_hash_password(password),
            real_name=real_name.strip(),
            mobile=mobile,
            id_card_hash=_hash_secret(id_card.upper()) if id_card else None,
            id_card_masked=_mask_id_card(id_card.upper()) if id_card else None,
            verification=verification.__dict__,
        )
        return self._with_session(user)

    def wechat_status(self) -> dict:
        configured = bool(self.settings.wechat_app_id and self.settings.wechat_redirect_uri)
        can_exchange_code = bool(configured and self.settings.wechat_app_secret)
        return {
            "configured": configured,
            "can_exchange_code": can_exchange_code,
            "mode": self.settings.wechat_login_mode,
            "requires_official_app": True,
            "required_env": ["WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_REDIRECT_URI"],
        }

    def wechat_login_url(self, mode: str | None = None, state: str | None = None, redirect_uri: str | None = None) -> dict:
        if not self.settings.wechat_app_id:
            raise AuthError("WECHAT_APP_ID is not configured")
        redirect = redirect_uri or self.settings.wechat_redirect_uri
        if not redirect:
            raise AuthError("WECHAT_REDIRECT_URI is not configured")
        login_mode = mode or self.settings.wechat_login_mode
        state_value = state or secrets.token_urlsafe(16)
        if login_mode == "mp":
            base = "https://open.weixin.qq.com/connect/oauth2/authorize"
            scope = "snsapi_userinfo"
        else:
            base = "https://open.weixin.qq.com/connect/qrconnect"
            scope = "snsapi_login"
        query = urlencode(
            {
                "appid": self.settings.wechat_app_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": scope,
                "state": state_value,
            }
        )
        return {"url": f"{base}?{query}#wechat_redirect", "state": state_value, "mode": login_mode, "scope": scope}

    async def wechat_callback(self, code: str, state: str | None = None) -> dict:
        if not self.settings.wechat_app_id or not self.settings.wechat_app_secret:
            raise AuthError("WECHAT_APP_ID and WECHAT_APP_SECRET are required to exchange code")
        async with httpx.AsyncClient(timeout=12.0) as client:
            token_resp = await client.get(
                "https://api.weixin.qq.com/sns/oauth2/access_token",
                params={
                    "appid": self.settings.wechat_app_id,
                    "secret": self.settings.wechat_app_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token = token_resp.json()
            if "errcode" in token:
                raise AuthError(f"wechat token exchange failed: {token}")
            openid = token["openid"]
            unionid = token.get("unionid")
            userinfo = {"openid": openid, "unionid": unionid}
            if token.get("access_token"):
                info_resp = await client.get(
                    "https://api.weixin.qq.com/sns/userinfo",
                    params={"access_token": token["access_token"], "openid": openid, "lang": "zh_CN"},
                )
                if info_resp.status_code == 200:
                    candidate = info_resp.json()
                    if "errcode" not in candidate:
                        userinfo.update(candidate)
        user = self.store.upsert_auth_identity(
            provider="wechat",
            provider_user_id=openid,
            display_name=userinfo.get("nickname") or "微信用户",
            union_id=unionid,
            raw=userinfo,
        )
        return {**self._with_session(user), "state": state}

    def _with_session(self, user: dict) -> dict:
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        self.store.create_session(user["id"], token, expires_at)
        safe_user = {"id": user["id"], "display_name": user["display_name"], "email": user.get("email"), "username": user.get("username"), "mobile": _mask_mobile(user.get("mobile")) if user.get("mobile") else None}
        return {"user": safe_user, "session": {"token": token, "expires_at": expires_at}}


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return "pbkdf2_sha256$200000$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(digest).decode("ascii")


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    return hmac.compare_digest(actual, expected)


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mask_id_card(value: str) -> str:
    if len(value) < 8:
        return "***"
    return value[:3] + "*" * (len(value) - 7) + value[-4:]


def _mask_mobile(value: str) -> str:
    return value[:3] + "****" + value[-4:] if len(value) == 11 else value
