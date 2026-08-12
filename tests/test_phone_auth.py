from pathlib import Path

import pytest

from personal_news_agent.config import Settings
from personal_news_agent.services.auth import AuthError, AuthService
from personal_news_agent.services.phone_verification import PhoneVerificationError, PhoneVerificationService
from personal_news_agent.services.store import NewsStore


def _auth(tmp_path: Path, **overrides) -> AuthService:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'phone-auth.db'}",
        seed_demo_data=False,
        phone_challenge_provider="mock",
        phone_challenge_secret="test-phone-challenge-secret-that-is-long-enough",
        phone_challenge_mock_enabled=True,
        phone_challenge_mock_code="123456",
        phone_challenge_resend_seconds=10,
        **overrides,
    )
    store = NewsStore(settings.sqlite_path)
    store.init()
    return AuthService(store, settings)


def test_phone_code_registration_login_and_one_time_consumption(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    mobile = "13800138000"

    challenge = auth.request_registration_code(mobile, "127.0.0.1")
    assert challenge["mobile_masked"] == "138****8000"
    assert challenge["debug_code"] == "123456"

    with auth.store.connect() as conn:
        stored = conn.execute(
            "SELECT * FROM pna_phone_verification_challenges WHERE challenge_id = ?",
            (challenge["challenge_id"],),
        ).fetchone()
    assert mobile not in tuple(str(value) for value in stored)
    assert "123456" not in tuple(str(value) for value in stored)

    registered = auth.register_phone(
        mobile=mobile,
        challenge_id=challenge["challenge_id"],
        verification_code="123456",
        password="12345678",
        confirm_password="12345678",
    )
    assert registered["user"]["mobile"] == "138****8000"
    assert registered["session"]["token"]

    logged_in = auth.login(mobile, "12345678")
    assert logged_in["user"]["id"] == registered["user"]["id"]

    with pytest.raises(PhoneVerificationError) as consumed:
        auth.phone_verification.verify_code(challenge["challenge_id"], mobile, "123456")
    assert getattr(consumed.value, "code", "") == "phone_code_consumed"

    with pytest.raises(AuthError) as reused:
        auth.register_phone(
            mobile=mobile,
            challenge_id=challenge["challenge_id"],
            verification_code="123456",
            password="12345678",
            confirm_password="12345678",
        )
    assert reused.value.code == "mobile_already_registered"


def test_delete_phone_user_previews_then_removes_account_sessions_and_challenges(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    mobile = "13800138000"
    challenge = auth.request_registration_code(mobile, "127.0.0.1")
    registered = auth.register_phone(
        mobile=mobile,
        challenge_id=challenge["challenge_id"],
        verification_code="123456",
        password="12345678",
        confirm_password="12345678",
    )
    user_id = registered["user"]["id"]
    mobile_hash = auth.phone_verification.mobile_hash(mobile)

    preview = auth.store.delete_phone_user(mobile, mobile_hash)
    assert preview["deleted"] is False
    assert preview["counts"]["pna_users"] == 1
    assert preview["counts"]["pna_auth_sessions"] == 1
    assert preview["counts"]["pna_phone_verification_challenges"] == 1
    assert auth.store.get_user(user_id) is not None

    deleted = auth.store.delete_phone_user(mobile, mobile_hash, confirm=True)
    assert deleted["deleted"] is True
    assert auth.store.get_user(user_id) is None
    with auth.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pna_auth_sessions WHERE user_id = ?", (user_id,)).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM pna_phone_verification_challenges WHERE mobile_hash = ?",
            (mobile_hash,),
        ).fetchone()[0] == 0


def test_delete_phone_user_does_not_require_verification_secret(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    mobile = "13800138000"
    challenge = auth.request_registration_code(mobile, "127.0.0.1")
    registered = auth.register_phone(
        mobile=mobile,
        challenge_id=challenge["challenge_id"],
        verification_code="123456",
        password="12345678",
        confirm_password="12345678",
    )
    user_id = registered["user"]["id"]

    preview = auth.store.delete_phone_user(mobile)
    assert preview["counts"]["pna_users"] == 1
    assert "pna_phone_verification_challenges" not in preview["counts"]

    deleted = auth.store.delete_phone_user(mobile, confirm=True)
    assert deleted["deleted"] is True
    assert auth.store.get_user(user_id) is None
    with auth.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pna_auth_sessions WHERE user_id = ?", (user_id,)).fetchone()[0] == 0


def test_change_phone_user_preserves_identity_password_session_and_owned_data(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    old_mobile = "13800138000"
    new_mobile = "13900139000"
    challenge = auth.request_registration_code(old_mobile, "127.0.0.1")
    registered = auth.register_phone(
        mobile=old_mobile,
        challenge_id=challenge["challenge_id"],
        verification_code="123456",
        password="12345678",
        confirm_password="12345678",
    )
    user_id = registered["user"]["id"]
    before = auth.store.get_user(user_id)
    with auth.store.connect() as conn:
        conn.execute(
            "INSERT INTO conversations(id, user_id, title, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("conv-change-phone", user_id, "保留的对话", "chat", "now", "now"),
        )

    preview = auth.store.change_phone_user(
        old_mobile,
        new_mobile,
        auth.phone_verification.mobile_hash(old_mobile),
        auth.phone_verification.mobile_hash(new_mobile),
    )
    assert preview["changed"] is False
    assert auth.store.get_user_by_mobile(old_mobile)["id"] == user_id

    changed = auth.store.change_phone_user(
        old_mobile,
        new_mobile,
        auth.phone_verification.mobile_hash(old_mobile),
        auth.phone_verification.mobile_hash(new_mobile),
        confirm=True,
    )
    assert changed["changed"] is True
    assert auth.store.get_user_by_mobile(old_mobile) is None
    after = auth.store.get_user_by_mobile(new_mobile)
    assert after["id"] == user_id
    assert after["password_hash"] == before["password_hash"]
    assert auth.login(new_mobile, "12345678")["user"]["id"] == user_id
    with auth.store.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE id = ? AND user_id = ?",
            ("conv-change-phone", user_id),
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM pna_auth_sessions WHERE user_id = ?", (user_id,)).fetchone()[0] >= 1


def test_phone_code_rejects_wrong_code_and_resend_during_cooldown(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    mobile = "13900139000"
    challenge = auth.request_registration_code(mobile, "127.0.0.2")

    with pytest.raises(AuthError) as wrong:
        auth.register_phone(
            mobile=mobile,
            challenge_id=challenge["challenge_id"],
            verification_code="654321",
            password="abcdefgh",
            confirm_password="abcdefgh",
        )
    assert wrong.value.code == "phone_code_invalid"

    with pytest.raises(AuthError) as cooldown:
        auth.request_registration_code(mobile, "127.0.0.2")
    assert cooldown.value.code == "phone_code_resend_too_soon"
    assert cooldown.value.status_code == 429


def test_phone_registration_requires_configured_provider(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'disabled-auth.db'}",
        seed_demo_data=False,
        phone_challenge_provider="disabled",
        phone_challenge_secret=None,
    )
    store = NewsStore(settings.sqlite_path)
    store.init()
    auth = AuthService(store, settings)

    assert auth.phone_registration_status()["available"] is False
    with pytest.raises(AuthError) as unavailable:
        auth.request_registration_code("13800138000", "127.0.0.1")
    assert unavailable.value.code == "phone_challenge_not_configured"
    assert unavailable.value.status_code == 503


def test_aliyun_phone_registration_reuses_provider_credentials_for_internal_hashing(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'aliyun-auth.db'}",
        seed_demo_data=False,
        phone_challenge_provider="aliyun_pnvs",
        phone_challenge_secret=None,
        aliyun_access_key_id="configured-access-key-id",
        aliyun_access_key_secret="configured-secret",
        pnvs_sign_name="速通互联验证码",
        pnvs_template_code="100001",
        pnvs_scheme_name="fin-agent-register",
    )
    store = NewsStore(settings.sqlite_path)
    store.init()
    auth = AuthService(store, settings)

    status = auth.phone_registration_status()
    assert status["available"] is True
    assert status["provider"] == "aliyun_pnvs"
    assert status["secret_configured"] is True
    assert status["provider_configured"] is True


@pytest.mark.parametrize("provider_code", ["BUSINESS_LIMIT_CONTROL", "FREQUENCY_FAIL"])
def test_aliyun_send_frequency_errors_are_public_rate_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_code: str,
) -> None:
    auth = _auth(tmp_path)
    service: PhoneVerificationService = auth.phone_verification
    object.__setattr__(service, "provider", "aliyun_pnvs")

    class Response:
        body = {"Code": provider_code, "Message": "provider detail", "RequestId": "request-123"}

    class Client:
        def send_sms_verify_code_with_options(self, request, runtime):
            return Response()

    monkeypatch.setattr(service, "_pnvs_client", lambda: Client())
    monkeypatch.setattr(service, "_pnvs_send_request", lambda mobile, challenge_id: object())
    monkeypatch.setattr(service, "_pnvs_runtime", lambda: object())

    with pytest.raises(PhoneVerificationError) as failure:
        service._send_code("13800138000", "pvc_test")

    assert failure.value.code == "phone_code_rate_limited"
    assert failure.value.status_code == 429


def test_aliyun_send_exception_is_logged_without_sensitive_request_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth = _auth(tmp_path)
    service: PhoneVerificationService = auth.phone_verification
    object.__setattr__(service, "provider", "aliyun_pnvs")

    class ProviderFailure(Exception):
        code = "INVALID_PARAMETERS"
        message = "parameter is not valid"
        data = {"RequestId": "request-456"}

    class Client:
        def send_sms_verify_code_with_options(self, request, runtime):
            raise ProviderFailure()

    monkeypatch.setattr(service, "_pnvs_client", lambda: Client())
    monkeypatch.setattr(service, "_pnvs_send_request", lambda mobile, challenge_id: object())
    monkeypatch.setattr(service, "_pnvs_runtime", lambda: object())

    with pytest.raises(PhoneVerificationError) as failure:
        service._send_code("13800138000", "pvc_sensitive_challenge")

    output = capsys.readouterr().out
    assert failure.value.code == "phone_code_send_unavailable"
    assert '"provider_code":"INVALID_PARAMETERS"' in output
    assert '"request_id":"request-456"' in output
    assert "13800138000" not in output
    assert "pvc_sensitive_challenge" not in output


def test_phone_registration_rejects_invalid_mobile_as_public_auth_error(tmp_path: Path) -> None:
    auth = _auth(tmp_path)
    with pytest.raises(AuthError) as invalid:
        auth.register_phone(
            mobile="12800138000",
            challenge_id="pvc_invalid_challenge",
            verification_code="123456",
            password="12345678",
            confirm_password="12345678",
        )
    assert invalid.value.code == "invalid_mobile"
    assert invalid.value.status_code == 400
