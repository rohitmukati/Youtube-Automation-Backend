from datetime import timedelta

from sqlalchemy import select

from app.models import PasswordResetOtp
from app.utils.datetime_utils import utc_now

API = "/api/v1"

VALID_SIGNUP = {
    "name": "Rohan Mukati",
    "email": "Rohan@Example.com",
    "password": "StrongPass123",
    "phone": "+91 98765-43210",
}


async def signup(client, **overrides):
    payload = {**VALID_SIGNUP, **overrides}
    return await client.post(f"{API}/auth/signup", json=payload)


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_signup_logs_user_in_directly(client):
    response = await signup(client)
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "rohan@example.com"
    assert body["user"]["name"] == "Rohan Mukati"
    assert body["user"]["phone"] == "+919876543210"
    assert "password" not in body["user"]
    assert "password_hash" not in body["user"]
    assert body["tokens"]["access_token"]
    assert body["tokens"]["refresh_token"]
    assert body["tokens"]["token_type"] == "bearer"


async def test_signup_without_phone(client):
    response = await signup(client, phone=None)
    assert response.status_code == 201
    assert response.json()["user"]["phone"] is None


async def test_signup_duplicate_email_is_rejected(client):
    assert (await signup(client)).status_code == 201
    response = await signup(client, email="rohan@example.com")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_registered"


async def test_signup_rejects_weak_password(client):
    response = await signup(client, password="onlyletters")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"][0]["field"] == "password"
    assert "letter and one number" in error["details"][0]["message"]


async def test_signup_rejects_short_password(client):
    response = await signup(client, password="Ab1")
    assert response.status_code == 422
    assert "at least" in response.json()["error"]["details"][0]["message"]


async def test_signup_rejects_invalid_email_and_phone(client):
    response = await signup(client, email="not-an-email", phone="12ab")
    assert response.status_code == 422
    fields = {item["field"] for item in response.json()["error"]["details"]}
    assert fields == {"email", "phone"}


async def test_signup_reports_missing_fields(client):
    response = await client.post(f"{API}/auth/signup", json={})
    assert response.status_code == 422
    fields = {item["field"] for item in response.json()["error"]["details"]}
    assert {"name", "email", "password"} <= fields


async def test_login_success_with_any_email_casing(client):
    await signup(client)
    response = await client.post(
        f"{API}/auth/login", json={"email": "ROHAN@example.com", "password": "StrongPass123"}
    )
    assert response.status_code == 200
    assert response.json()["tokens"]["access_token"]


async def test_login_wrong_password_and_unknown_email_look_the_same(client):
    await signup(client)
    wrong_password = await client.post(
        f"{API}/auth/login", json={"email": "rohan@example.com", "password": "WrongPass123"}
    )
    unknown_email = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "WrongPass123"}
    )
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["error"]["message"] == "Invalid email or password."


async def test_me_requires_valid_token(client):
    missing = await client.get(f"{API}/users/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "not_authenticated"

    garbage = await client.get(f"{API}/users/me", headers=auth_header("garbage.token.value"))
    assert garbage.status_code == 401
    assert garbage.json()["error"]["code"] == "invalid_token"


async def test_me_returns_current_user(client):
    tokens = (await signup(client)).json()["tokens"]
    response = await client.get(f"{API}/users/me", headers=auth_header(tokens["access_token"]))
    assert response.status_code == 200
    assert response.json()["email"] == "rohan@example.com"


async def test_refresh_rotates_token_and_detects_reuse(client):
    tokens = (await signup(client)).json()["tokens"]
    first = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 200
    new_tokens = first.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    reused = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reused.status_code == 401

    revoked_family = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert revoked_family.status_code == 401


async def test_refresh_rejects_unknown_token(client):
    response = await client.post(f"{API}/auth/refresh", json={"refresh_token": "unknown-token"})
    assert response.status_code == 401


async def test_logout_revokes_refresh_token(client):
    tokens = (await signup(client)).json()["tokens"]
    logout = await client.post(f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert logout.status_code == 200
    again = await client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401


async def test_forgot_password_sends_otp_to_existing_user(client, email_service):
    await signup(client)
    response = await client.post(f"{API}/auth/forgot-password", json={"email": "rohan@example.com"})
    assert response.status_code == 200
    assert len(email_service.sent) == 1
    delivery = email_service.sent[0]
    assert delivery.email == "rohan@example.com"
    assert len(delivery.otp) == 6 and delivery.otp.isdigit()


async def test_forgot_password_does_not_reveal_unknown_email(client, email_service):
    known = await client.post(f"{API}/auth/forgot-password", json={"email": "nobody@example.com"})
    await signup(client)
    existing = await client.post(f"{API}/auth/forgot-password", json={"email": "rohan@example.com"})
    assert known.status_code == existing.status_code == 200
    assert known.json() == existing.json()
    assert len(email_service.sent) == 1


async def test_forgot_password_respects_resend_cooldown(client, email_service):
    await signup(client)
    for _ in range(3):
        response = await client.post(f"{API}/auth/forgot-password", json={"email": "rohan@example.com"})
        assert response.status_code == 200
    assert len(email_service.sent) == 1


async def test_login_does_not_send_any_otp(client, email_service):
    await signup(client)
    await client.post(
        f"{API}/auth/login", json={"email": "rohan@example.com", "password": "StrongPass123"}
    )
    assert email_service.sent == []


async def request_otp(client, email_service) -> str:
    await client.post(f"{API}/auth/forgot-password", json={"email": "rohan@example.com"})
    return email_service.sent[-1].otp


async def test_reset_password_full_flow(client, email_service):
    tokens = (await signup(client)).json()["tokens"]
    otp = await request_otp(client, email_service)

    reset = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "rohan@example.com", "otp": otp, "new_password": "BrandNew456"},
    )
    assert reset.status_code == 200

    old_login = await client.post(
        f"{API}/auth/login", json={"email": "rohan@example.com", "password": "StrongPass123"}
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        f"{API}/auth/login", json={"email": "rohan@example.com", "password": "BrandNew456"}
    )
    assert new_login.status_code == 200

    old_session = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert old_session.status_code == 401

    reuse = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "rohan@example.com", "otp": otp, "new_password": "AnotherPass789"},
    )
    assert reuse.status_code == 400
    assert reuse.json()["error"]["code"] == "invalid_otp"


async def test_reset_password_rejects_wrong_otp(client, email_service):
    await signup(client)
    otp = await request_otp(client, email_service)
    wrong = "000000" if otp != "000000" else "111111"
    response = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "rohan@example.com", "otp": wrong, "new_password": "BrandNew456"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_otp"


async def test_reset_password_locks_after_too_many_attempts(client, email_service):
    await signup(client)
    otp = await request_otp(client, email_service)
    wrong = "000000" if otp != "000000" else "111111"
    for _ in range(5):
        attempt = await client.post(
            f"{API}/auth/reset-password",
            json={"email": "rohan@example.com", "otp": wrong, "new_password": "BrandNew456"},
        )
        assert attempt.status_code == 400
    locked = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "rohan@example.com", "otp": otp, "new_password": "BrandNew456"},
    )
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "too_many_attempts"


async def test_reset_password_rejects_expired_otp(client, email_service, session_factory):
    await signup(client)
    otp = await request_otp(client, email_service)
    async with session_factory() as session:
        record = (await session.execute(select(PasswordResetOtp))).scalar_one()
        record.expires_at = utc_now() - timedelta(minutes=1)
        await session.commit()
    response = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "rohan@example.com", "otp": otp, "new_password": "BrandNew456"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_otp"


async def test_reset_password_validates_otp_format_and_new_password(client):
    response = await client.post(
        f"{API}/auth/reset-password",
        json={"email": "rohan@example.com", "otp": "12ab", "new_password": "short"},
    )
    assert response.status_code == 422
    fields = {item["field"] for item in response.json()["error"]["details"]}
    assert fields == {"otp", "new_password"}


async def test_unknown_route_uses_standard_error_format(client):
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["success"] is False


async def test_health_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
