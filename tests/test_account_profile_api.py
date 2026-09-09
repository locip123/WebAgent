from fastapi.testclient import TestClient

from browser_use.webretriever.desktop.api import create_app
from browser_use.webretriever.desktop.account_profile_store import AccountProfileStore


def _client(profile_path):
	return TestClient(
		create_app(
			manager=object(),
			launch_token="test-token",
			account_profile_store=AccountProfileStore(profile_path),
		)
	)


def _authorized_headers():
	return {"Authorization": "Bearer test-token"}


def test_account_profile_requires_the_sidecar_bearer_token(tmp_path) -> None:
	response = _client(tmp_path / "account-profile.json").get("/api/v1/account-profile")

	assert response.status_code == 401
	assert response.json()["error_code"] == "unauthorized"


def test_account_profile_can_be_saved_and_loaded_after_sidecar_restart(tmp_path) -> None:
	profile_path = tmp_path / "account-profile.json"
	profile = {
		"name": "林晓宇",
		"email": "xiaoyu@example.com",
		"age": 29,
		"work": "产品设计师",
		"organization": "webAgent",
	}

	with _client(profile_path) as client:
		updated = client.put("/api/v1/account-profile", headers=_authorized_headers(), json=profile)

	assert updated.status_code == 200
	assert updated.json() == profile

	with _client(profile_path) as restarted_client:
		loaded = restarted_client.get("/api/v1/account-profile", headers=_authorized_headers())

	assert loaded.status_code == 200
	assert loaded.json() == profile


def test_account_profile_rejects_an_invalid_email_or_age(tmp_path) -> None:
	client = _client(tmp_path / "account-profile.json")

	response = client.put(
		"/api/v1/account-profile",
		headers=_authorized_headers(),
		json={"name": "林晓宇", "email": "not-an-email", "age": -1, "work": "设计师", "organization": "webAgent"},
	)

	assert response.status_code == 422
	assert response.json()["error_code"] == "validation_failed"
