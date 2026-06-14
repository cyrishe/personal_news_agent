from __future__ import annotations

import re
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from personal_news_agent.config import Settings


class RealNameVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class RealNameVerificationResult:
    passed: bool
    provider: str
    request_id: str
    message: str
    verified_at: str


class RealNameVerificationService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def verify(self, real_name: str, id_card: str | None, mobile: str) -> RealNameVerificationResult:
        provider = self.settings.realname_provider
        if provider == "mock":
            return self._mock_verify(real_name, id_card or "110101199001011234", mobile)
        if provider == "aliyun":
            self._ensure_aliyun_configured()
            return self._aliyun_mobile_2meta_verify(real_name, mobile)
        if provider == "tencent":
            self._ensure_tencent_configured()
            return self._tencent_check_phone_and_name(real_name, mobile)
        raise RealNameVerificationError(f"unsupported real-name provider: {provider}")

    def status(self) -> dict:
        provider = self.settings.realname_provider
        status = {
            "provider": self.settings.realname_provider,
            "provider_name": {"mock": "Mock", "aliyun": "阿里云", "tencent": "腾讯云"}.get(provider, provider),
            "test_identity_configured": bool(self.settings.realname_test_name and self.settings.realname_test_mobile),
            "required_user_fields": ["real_name", "mobile"],
        }
        if provider == "mock":
            status["mock_enabled"] = self.settings.realname_mock_enabled
        elif provider == "aliyun":
            status.update(
                {
                    "aliyun_configured": bool(self.settings.aliyun_access_key_id and self.settings.aliyun_access_key_secret),
                    "aliyun_endpoint": self.settings.aliyun_cloudauth_endpoint,
                    "aliyun_region_id": self.settings.aliyun_region_id,
                }
            )
        elif provider == "tencent":
            status.update(
                {
                    "tencent_configured": bool(self.settings.tencent_secret_id and self.settings.tencent_secret_key),
                    "tencent_app_id_present": bool(self.settings.tencent_app_id),
                    "tencent_endpoint": self.settings.tencent_faceid_endpoint,
                }
            )
        return status

    def _mock_verify(self, real_name: str, id_card: str, mobile: str) -> RealNameVerificationResult:
        if not self.settings.realname_mock_enabled:
            raise RealNameVerificationError("mock real-name provider is disabled")
        if len(real_name.strip()) < 2:
            raise RealNameVerificationError("真实姓名至少需要 2 个字。")
        if not re.fullmatch(r"1[3-9]\d{9}", mobile):
            raise RealNameVerificationError("手机号格式不正确，请输入 11 位中国大陆手机号。")
        return RealNameVerificationResult(
            passed=True,
            provider="mock",
            request_id=f"mock_{mobile[-4:]}",
            message="mock verification passed",
            verified_at=datetime.now(timezone.utc).isoformat(),
        )

    def _ensure_aliyun_configured(self) -> None:
        if not (self.settings.aliyun_access_key_id and self.settings.aliyun_access_key_secret):
            raise RealNameVerificationError("aliyun provider requires ALIYUN_ACCESS_KEY_ID/ALIYUN_ACCESS_KEY_SECRET or ALIBABA_CLOUD_ACCESS_KEY_ID/ALIBABA_CLOUD_ACCESS_KEY_SECRET")

    def _ensure_tencent_configured(self) -> None:
        if not (self.settings.tencent_secret_id and self.settings.tencent_secret_key):
            raise RealNameVerificationError("tencent provider requires TENCENT_SECRET_ID and TENCENT_SECRET_KEY")

    def _aliyun_mobile_2meta_verify(self, real_name: str, mobile: str) -> RealNameVerificationResult:
        if not re.fullmatch(r"1[3-9]\d{9}", mobile):
            raise RealNameVerificationError("手机号格式不正确，请输入 11 位中国大陆手机号。")
        try:
            from alibabacloud_cloudauth20190307.client import Client as CloudAuthClient
            from alibabacloud_cloudauth20190307 import models as cloudauth_models
            from alibabacloud_tea_openapi import models as openapi_models
        except ImportError as exc:
            raise RealNameVerificationError("aliyun provider requires alibabacloud_cloudauth20190307 SDK") from exc

        config = openapi_models.Config(
            access_key_id=self.settings.aliyun_access_key_id,
            access_key_secret=self.settings.aliyun_access_key_secret,
            endpoint=self.settings.aliyun_cloudauth_endpoint,
            region_id=self.settings.aliyun_region_id,
        )
        client = CloudAuthClient(config)
        request = cloudauth_models.Mobile2MetaVerifyRequest(
            mobile=mobile,
            param_type="normal",
            user_name=real_name.strip(),
        )
        try:
            response = client.mobile_2meta_verify(request)
        except Exception as exc:
            raise RealNameVerificationError(str(exc)) from exc

        body = response.body
        code = str(getattr(body, "code", "") or "")
        message = str(getattr(body, "message", "") or "")
        request_id = str(getattr(body, "request_id", "") or "")
        result_object = getattr(body, "result_object", None)
        biz_code = str(getattr(result_object, "biz_code", "") or "")
        isp_name = str(getattr(result_object, "isp_name", "") or "")
        if code != "200":
            raise RealNameVerificationError(f"{code}: {message}")
        result_messages = {
            "1": "实名手机号核验通过",
            "2": "实名手机号核验不一致，请确认手机号是否为本人实名号码。",
            "3": "运营商未返回该姓名和手机号的实名记录，请确认号码是否已实名、是否刚办理或携号转网。",
        }
        detail = result_messages.get(biz_code, f"BizCode={biz_code}")
        if isp_name:
            detail = f"{detail}（运营商：{isp_name}）"
        return RealNameVerificationResult(
            passed=biz_code == "1",
            provider="aliyun",
            request_id=request_id,
            message=detail,
            verified_at=datetime.now(timezone.utc).isoformat(),
        )

    def _tencent_check_phone_and_name(self, real_name: str, mobile: str) -> RealNameVerificationResult:
        if not re.fullmatch(r"1[3-9]\d{9}", mobile):
            raise RealNameVerificationError("手机号格式不正确，请输入 11 位中国大陆手机号。")
        payload = {"Mobile": mobile, "Name": real_name.strip()}
        response = _tencent_cloud_post(
            endpoint=self.settings.tencent_faceid_endpoint,
            secret_id=self.settings.tencent_secret_id or "",
            secret_key=self.settings.tencent_secret_key or "",
            service="faceid",
            action="CheckPhoneAndName",
            version="2018-03-01",
            payload=payload,
        )
        body = response.get("Response", {})
        if "Error" in body:
            error = body["Error"]
            raise RealNameVerificationError(f"{error.get('Code')}: {error.get('Message')}")
        result = str(body.get("Result", ""))
        description = body.get("Description") or ""
        return RealNameVerificationResult(
            passed=result == "0",
            provider="tencent",
            request_id=body.get("RequestId", ""),
            message=description or f"Result={result}",
            verified_at=datetime.now(timezone.utc).isoformat(),
        )


def _tencent_cloud_post(endpoint: str, secret_id: str, secret_key: str, service: str, action: str, version: str, payload: dict) -> dict:
    parsed = urlparse(endpoint)
    host = parsed.netloc
    timestamp = int(datetime.now(timezone.utc).timestamp())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    algorithm = "TC3-HMAC-SHA256"
    http_request_method = "POST"
    canonical_uri = "/"
    canonical_querystring = ""
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\nx-tc-action:{action.lower()}\n"
    signed_headers = "content-type;host;x-tc-action"
    hashed_request_payload = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    canonical_request = "\n".join(
        [
            http_request_method,
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            hashed_request_payload,
        ]
    )
    credential_scope = f"{date}/{service}/tc3_request"
    hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "\n".join([algorithm, str(timestamp), credential_scope, hashed_canonical_request])
    secret_date = _sign(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _sign(secret_date, service)
    secret_signing = _sign(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"{algorithm} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": version,
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(endpoint, content=payload_json.encode("utf-8"), headers=headers)
        resp.raise_for_status()
        return resp.json()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
