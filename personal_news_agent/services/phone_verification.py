from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from personal_news_agent.config import Settings
from personal_news_agent.services.store import NewsStore


_MOBILE_PATTERN = re.compile(r"1[3-9][0-9]{9}")
_CODE_PATTERN = re.compile(r"[0-9]{6}")
_CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,96}")


class PhoneVerificationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class PhonePossessionProof:
    challenge_id: str
    mobile_hash: str
    provider: str


def normalize_mainland_mobile(value: Any) -> str:
    compact = re.sub(r"[\s()-]", "", str(value or "").strip())
    if compact.startswith("+86"):
        compact = compact[3:]
    elif compact.startswith("0086"):
        compact = compact[4:]
    if not _MOBILE_PATTERN.fullmatch(compact):
        raise PhoneVerificationError("invalid_mobile", "请输入有效的 11 位中国大陆手机号。")
    return compact


class PhoneVerificationService:
    """Durable registration challenges with mock and Aliyun PNVS adapters."""

    def __init__(self, store: NewsStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.provider = settings.phone_challenge_provider.strip().lower()
        # The Aliyun credential already provides process-private key material.
        # Requiring a second HMAC secret only duplicated deployment config.
        hmac_secret = settings.phone_challenge_secret or settings.aliyun_access_key_secret or ""
        self._secret = hmac_secret.encode("utf-8")
        self.ttl_seconds = min(1800, max(60, settings.phone_challenge_ttl_seconds))
        self.resend_seconds = min(600, max(10, settings.phone_challenge_resend_seconds))
        self.max_attempts = min(10, max(1, settings.phone_challenge_max_attempts))
        self.rate_window_seconds = min(86400, max(60, settings.phone_challenge_rate_window_seconds))
        self.mobile_rate_limit = min(100, max(1, settings.phone_challenge_mobile_rate_limit))
        self.ip_rate_limit = min(1000, max(1, settings.phone_challenge_ip_rate_limit))

    def status(self) -> dict[str, Any]:
        provider_supported = self.provider in {"disabled", "mock", "aliyun_pnvs"}
        secret_configured = bool(self._secret)
        if self.provider == "mock":
            provider_configured = self.settings.phone_challenge_mock_enabled and bool(
                _CODE_PATTERN.fullmatch(self.settings.phone_challenge_mock_code)
            )
        elif self.provider == "aliyun_pnvs":
            provider_configured = bool(
                self.settings.aliyun_access_key_id
                and self.settings.aliyun_access_key_secret
                and self.settings.pnvs_sign_name
                and self.settings.pnvs_template_code
                and self.settings.pnvs_scheme_name
                and len(self.settings.pnvs_scheme_name) <= 20
            )
        else:
            provider_configured = self.provider == "disabled"
        enabled = self.provider != "disabled" and provider_supported and secret_configured and provider_configured
        return {
            "available": enabled,
            "provider": self.provider,
            "method": "sms_otp",
            "secret_configured": secret_configured,
            "provider_configured": provider_configured,
            "required_registration_fields": [
                "mobile",
                "challenge_id",
                "verification_code",
                "password",
                "confirm_password",
            ],
        }

    def mobile_hash(self, mobile: Any) -> str:
        """Return the same non-reversible mobile key used by stored challenges."""

        normalized_mobile = normalize_mainland_mobile(mobile)
        return self._hmac(f"mobile:{normalized_mobile}")

    def request_code(self, mobile: Any, remote_addr: str = "") -> dict[str, Any]:
        self._require_operational()
        normalized_mobile = normalize_mainland_mobile(mobile)
        if self.store.get_user_by_mobile(normalized_mobile):
            raise PhoneVerificationError(
                "mobile_already_registered",
                "该手机号已经注册，请直接登录。",
                status_code=409,
            )

        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        rate_cutoff = (now - timedelta(seconds=self.rate_window_seconds)).isoformat()
        challenge_id = f"pvc_{secrets.token_urlsafe(24)}"
        mobile_hash = self.mobile_hash(normalized_mobile)
        ip_hash = self._hmac(f"ip:{str(remote_addr or 'unknown')[:64]}")
        code = self.settings.phone_challenge_mock_code if self.provider == "mock" else ""
        code_hash = self._code_hash(challenge_id, mobile_hash, code) if code else "provider-managed"
        expires_at = (now + timedelta(seconds=self.ttl_seconds)).isoformat()
        resend_after = (now + timedelta(seconds=self.resend_seconds)).isoformat()

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cooldown = conn.execute(
                    """
                    SELECT resend_after
                    FROM pna_phone_verification_challenges
                    WHERE mobile_hash = ?
                      AND send_succeeded_at IS NOT NULL
                      AND consumed_at IS NULL
                      AND resend_after > ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (mobile_hash, now_text),
                ).fetchone()
                if cooldown:
                    retry_after = max(1, int((_parse_time(cooldown["resend_after"]) - now).total_seconds()))
                    raise PhoneVerificationError(
                        "phone_code_resend_too_soon",
                        f"验证码已发送，请在 {retry_after} 秒后重试。",
                        status_code=429,
                        retry_after_seconds=retry_after,
                    )
                mobile_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM pna_phone_verification_challenges WHERE mobile_hash = ? AND created_at >= ?",
                    (mobile_hash, rate_cutoff),
                ).fetchone()["count"]
                ip_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM pna_phone_verification_challenges WHERE request_ip_hash = ? AND created_at >= ?",
                    (ip_hash, rate_cutoff),
                ).fetchone()["count"]
                if mobile_count >= self.mobile_rate_limit or ip_count >= self.ip_rate_limit:
                    raise PhoneVerificationError(
                        "phone_code_rate_limited",
                        "验证码请求过于频繁，请稍后再试。",
                        status_code=429,
                        retry_after_seconds=self.rate_window_seconds,
                    )
                conn.execute(
                    "UPDATE pna_phone_verification_challenges SET expires_at = ?, updated_at = ? WHERE mobile_hash = ? AND consumed_at IS NULL AND expires_at > ?",
                    (now_text, now_text, mobile_hash, now_text),
                )
                conn.execute(
                    """
                    INSERT INTO pna_phone_verification_challenges(
                      challenge_id, mobile_hash, code_hash, purpose, provider,
                      request_ip_hash, expires_at, resend_after, max_attempts,
                      created_at, updated_at
                    ) VALUES (?, ?, ?, 'registration', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        challenge_id,
                        mobile_hash,
                        code_hash,
                        self.provider,
                        ip_hash,
                        expires_at,
                        resend_after,
                        self.max_attempts,
                        now_text,
                        now_text,
                    ),
                )
            except Exception:
                conn.rollback()
                raise

        try:
            provider_request_id = self._send_code(normalized_mobile, challenge_id)
        except Exception as exc:
            with self.store.connect() as conn:
                conn.execute(
                    "UPDATE pna_phone_verification_challenges SET send_failed_at = ?, updated_at = ? WHERE challenge_id = ?",
                    (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), challenge_id),
                )
            if isinstance(exc, PhoneVerificationError):
                raise
            raise PhoneVerificationError(
                "phone_code_send_unavailable",
                "验证码发送服务暂时不可用，请稍后重试。",
                status_code=503,
            ) from None

        sent_at = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as conn:
            conn.execute(
                """
                UPDATE pna_phone_verification_challenges
                SET provider_request_id = ?, send_succeeded_at = ?, updated_at = ?
                WHERE challenge_id = ? AND send_failed_at IS NULL
                """,
                (provider_request_id, sent_at, sent_at, challenge_id),
            )
        result = {
            "challenge_id": challenge_id,
            "mobile_masked": normalized_mobile[:3] + "****" + normalized_mobile[-4:],
            "expires_in_seconds": self.ttl_seconds,
            "resend_after_seconds": self.resend_seconds,
        }
        if self.provider == "mock":
            result["debug_code"] = code
        return result

    def verify_code(self, challenge_id: Any, mobile: Any, code: Any) -> PhonePossessionProof:
        self._require_operational()
        normalized_challenge_id = str(challenge_id or "").strip()
        normalized_mobile = normalize_mainland_mobile(mobile)
        normalized_code = str(code or "").strip()
        if not _CHALLENGE_PATTERN.fullmatch(normalized_challenge_id) or not _CODE_PATTERN.fullmatch(normalized_code):
            raise PhoneVerificationError("phone_code_invalid", "验证码无效，请重新获取。")
        mobile_hash = self._hmac(f"mobile:{normalized_mobile}")
        now = datetime.now(timezone.utc)
        row = self._load_verifiable(normalized_challenge_id, mobile_hash, now)

        if self.provider == "mock":
            expected = self._code_hash(normalized_challenge_id, mobile_hash, normalized_code)
            verified = hmac.compare_digest(str(row["code_hash"]), expected)
        else:
            verified = self._verify_pnvs(normalized_mobile, normalized_code, normalized_challenge_id)
        if not verified:
            self._record_failed_attempt(normalized_challenge_id, int(row["attempt_count"]), int(row["max_attempts"]))

        verified_at = now.isoformat()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE pna_phone_verification_challenges SET verified_at = COALESCE(verified_at, ?), updated_at = ? WHERE challenge_id = ?",
                (verified_at, verified_at, normalized_challenge_id),
            )
        return PhonePossessionProof(normalized_challenge_id, mobile_hash, self.provider)

    def _load_verifiable(self, challenge_id: str, mobile_hash: str, now: datetime) -> dict[str, Any]:
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM pna_phone_verification_challenges WHERE challenge_id = ? AND mobile_hash = ? LIMIT 1",
                (challenge_id, mobile_hash),
            ).fetchone()
        if not row or row["purpose"] != "registration":
            raise PhoneVerificationError("phone_code_invalid", "验证码无效，请重新获取。")
        if row["consumed_at"]:
            raise PhoneVerificationError("phone_code_consumed", "该验证码已经使用，请重新获取。")
        if not row["send_succeeded_at"] or row["send_failed_at"]:
            raise PhoneVerificationError("phone_code_not_ready", "验证码尚未成功发送，请重新获取。")
        if _parse_time(row["expires_at"]) <= now:
            raise PhoneVerificationError("phone_code_expired", "验证码已过期，请重新获取。")
        if int(row["attempt_count"]) >= int(row["max_attempts"]):
            raise PhoneVerificationError("phone_code_attempts_exceeded", "验证码尝试次数过多，请重新获取。")
        return dict(row)

    def _record_failed_attempt(self, challenge_id: str, attempt_count: int, max_attempts: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE pna_phone_verification_challenges SET attempt_count = attempt_count + 1, updated_at = ? WHERE challenge_id = ?",
                (now, challenge_id),
            )
        if attempt_count + 1 >= max_attempts:
            raise PhoneVerificationError("phone_code_attempts_exceeded", "验证码尝试次数过多，请重新获取。")
        raise PhoneVerificationError("phone_code_invalid", "验证码不正确，请检查后重试。")

    def _send_code(self, mobile: str, challenge_id: str) -> str:
        if self.provider == "mock":
            return f"mock:{challenge_id}"
        try:
            response = self._pnvs_client().send_sms_verify_code_with_options(
                self._pnvs_send_request(mobile, challenge_id),
                self._pnvs_runtime(),
            )
        except Exception as exc:
            provider_code = _provider_exception_value(exc, "code", "Code")
            provider_message = _provider_exception_value(exc, "message", "Message")
            request_id = _provider_exception_value(exc, "request_id", "requestId", "RequestId")
            _log_provider_failure(
                phase="send",
                provider_code=provider_code or type(exc).__name__,
                provider_message=provider_message,
                request_id=request_id,
            )
            raise _provider_send_error(provider_code) from None
        body = _read_value(response, "body") or response
        provider_code = str(_read_value(body, "code", "Code") or "")
        if provider_code.upper() != "OK":
            _log_provider_failure(
                phase="send",
                provider_code=provider_code or "unknown",
                provider_message=str(_read_value(body, "message", "Message") or ""),
                request_id=str(_read_value(body, "request_id", "requestId", "RequestId") or ""),
            )
            raise _provider_send_error(provider_code)
        return str(_read_value(body, "request_id", "requestId", "RequestId") or "")

    def _verify_pnvs(self, mobile: str, code: str, challenge_id: str) -> bool:
        try:
            response = self._pnvs_client().check_sms_verify_code_with_options(
                self._pnvs_verify_request(mobile, code, challenge_id),
                self._pnvs_runtime(),
            )
        except Exception:
            raise PhoneVerificationError(
                "phone_code_verify_unavailable",
                "验证码校验服务暂时不可用，请稍后重试。",
                status_code=503,
            ) from None
        body = _read_value(response, "body") or response
        if str(_read_value(body, "code", "Code") or "").upper() != "OK":
            raise PhoneVerificationError(
                "phone_code_verify_unavailable",
                "验证码校验服务暂时不可用，请稍后重试。",
                status_code=503,
            )
        model = _read_value(body, "model", "Model")
        return str(_read_value(model, "verify_result", "verifyResult", "VerifyResult") or "").upper() == "PASS"

    def _pnvs_client(self) -> Any:
        from alibabacloud_dypnsapi20170525.client import Client
        from alibabacloud_tea_openapi import models as open_api_models

        config = open_api_models.Config(
            access_key_id=self.settings.aliyun_access_key_id,
            access_key_secret=self.settings.aliyun_access_key_secret,
        )
        config.endpoint = self.settings.pnvs_endpoint
        return Client(config)

    def _pnvs_runtime(self) -> Any:
        from alibabacloud_tea_util import models as util_models

        return util_models.RuntimeOptions(
            autoretry=False,
            max_attempts=1,
            connect_timeout=3000,
            read_timeout=5000,
        )

    def _pnvs_send_request(self, mobile: str, challenge_id: str) -> Any:
        from alibabacloud_dypnsapi20170525 import models as pnvs_models

        return pnvs_models.SendSmsVerifyCodeRequest(
            phone_number=mobile,
            country_code="86",
            sign_name=self.settings.pnvs_sign_name,
            template_code=self.settings.pnvs_template_code,
            template_param=json.dumps({"code": "##code##", "min": "5"}, separators=(",", ":")),
            scheme_name=self.settings.pnvs_scheme_name,
            out_id=challenge_id,
            code_length=6,
            valid_time=self.ttl_seconds,
            interval=self.resend_seconds,
            duplicate_policy=1,
            code_type=1,
            return_verify_code=False,
            auto_retry=1,
        )

    def _pnvs_verify_request(self, mobile: str, code: str, challenge_id: str) -> Any:
        from alibabacloud_dypnsapi20170525 import models as pnvs_models

        return pnvs_models.CheckSmsVerifyCodeRequest(
            phone_number=mobile,
            country_code="86",
            verify_code=code,
            scheme_name=self.settings.pnvs_scheme_name,
            out_id=challenge_id,
            case_auth_policy=2,
        )

    def _require_operational(self) -> None:
        status = self.status()
        if not status["available"]:
            raise PhoneVerificationError(
                "phone_challenge_not_configured",
                "短信验证码服务尚未完成配置。",
                status_code=503,
            )

    def _hmac(self, value: str) -> str:
        return hmac.new(self._secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _code_hash(self, challenge_id: str, mobile_hash: str, code: str) -> str:
        return self._hmac(f"code:{challenge_id}:{mobile_hash}:{code}")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_value(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _provider_exception_value(exc: Exception, *names: str) -> str:
    direct = _read_value(exc, *names)
    if direct not in (None, ""):
        return str(direct)
    data = _read_value(exc, "data")
    nested = _read_value(data, *names)
    return str(nested or "")


def _provider_send_error(provider_code: str) -> PhoneVerificationError:
    normalized = str(provider_code or "").strip().upper()
    if normalized in {"BUSINESS_LIMIT_CONTROL", "FREQUENCY_FAIL", "THROTTLING", "THROTTLING.USER"}:
        return PhoneVerificationError(
            "phone_code_rate_limited",
            "验证码请求过于频繁，请稍后再试。",
            status_code=429,
            retry_after_seconds=60,
        )
    if normalized == "MOBILE_NUMBER_ILLEGAL":
        return PhoneVerificationError("invalid_mobile", "请输入有效的 11 位中国大陆手机号。")
    return PhoneVerificationError(
        "phone_code_send_unavailable",
        "验证码发送服务暂时不可用，请稍后重试。",
        status_code=503,
    )


def _log_provider_failure(
    *,
    phase: str,
    provider_code: str,
    provider_message: str = "",
    request_id: str = "",
) -> None:
    # Deliberately excludes phone numbers, verification codes and credentials.
    payload = {
        "event": "phone_provider_failure",
        "provider": "aliyun_pnvs",
        "phase": phase,
        "provider_code": str(provider_code or "unknown")[:120],
    }
    if provider_message:
        payload["provider_message"] = str(provider_message)[:300]
    if request_id:
        payload["request_id"] = str(request_id)[:160]
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
