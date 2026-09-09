from fastapi.testclient import TestClient

import json

from browser_use.webretriever.desktop.api import create_app
from browser_use.webretriever.desktop.model_service_store import ModelServiceStore
from browser_use.webretriever.desktop.runner_adapter import JsonProfileResolver


def _authorized_headers() -> dict[str, str]:
	return {"Authorization": "Bearer test-token"}


def test_model_service_is_persisted_with_defaults_without_returning_its_api_key(tmp_path) -> None:
	config_path = tmp_path / "config.json"
	client = TestClient(
		create_app(
			manager=object(),
			launch_token="test-token",
			model_service_store=ModelServiceStore(config_path),
		)
	)

	response = client.post(
		"/api/v1/model-services",
		headers=_authorized_headers(),
		json={
			"name": "response",
			"api_base": "https://api.example.com/v1",
			"api_key": "test-key",
		},
	)

	assert response.status_code == 201
	assert response.json() == {
		"name": "response",
		"api_base": "https://api.example.com/v1",
		"model": "gpt-5.5",
		"response_mode": "responses",
	}
	assert "api_key" not in response.json()
	listed = client.get("/api/v1/model-services", headers=_authorized_headers())
	assert listed.status_code == 200
	assert listed.json() == [response.json()]
	assert "api_key" not in listed.json()[0]
	assert ModelServiceStore(config_path).load_raw_services() == [
		{
			"name": "response",
			"api_base": "https://api.example.com/v1",
			"api_key": "test-key",
			"model": "gpt-5.5",
			"response_mode": "responses",
		}
	]


def test_model_service_connection_test_uses_the_selected_model_and_response_mode(tmp_path) -> None:
	called_with = []

	async def test_connection(service) -> None:
		called_with.append(service)

	client = TestClient(
		create_app(
			manager=object(),
			launch_token="test-token",
			model_service_store=ModelServiceStore(tmp_path / "config.json"),
			model_service_tester=test_connection,
		)
	)

	response = client.post(
		"/api/v1/model-services/test",
		headers=_authorized_headers(),
		json={
			"name": "response",
			"api_base": "https://api.example.com/v1",
			"api_key": "test-key",
			"model": "gpt-5.4",
			"response_mode": "responses",
		},
	)

	assert response.status_code == 200
	assert response.json() == {"name": "response", "success": True, "error_code": None}
	assert called_with[0].model == "gpt-5.4"
	assert called_with[0].response_mode == "responses"


def test_saved_model_service_connection_test_keeps_the_api_key_server_side(tmp_path) -> None:
	called_with = []

	async def test_connection(service) -> None:
		called_with.append(service)

	client = TestClient(
		create_app(
			manager=object(),
			launch_token="test-token",
			model_service_store=ModelServiceStore(tmp_path / "config.json"),
			model_service_tester=test_connection,
		)
	)
	client.post(
		"/api/v1/model-services",
		headers=_authorized_headers(),
		json={"name": "response", "api_base": "https://api.example.com/v1", "api_key": "test-key"},
	)

	response = client.post("/api/v1/model-services/response/test", headers=_authorized_headers())

	assert response.status_code == 200
	assert response.json() == {"name": "response", "success": True, "error_code": None}
	assert called_with[0].api_key == "test-key"


def test_runner_profile_uses_each_service_model_and_response_mode_from_config(tmp_path) -> None:
	config_path = tmp_path / "config.json"
	config_path.write_text(
		json.dumps(
			{
				"api_model": "gpt-5.4",
				"model_services": [
					{
						"name": "chat-service",
						"api_base": "https://api.example.com/v1",
						"api_key": "test-key",
						"model": "gpt-5.4-mini",
						"response_mode": "chat-completions",
					}
				],
			}
		),
		encoding="utf-8",
	)

	profile = JsonProfileResolver({"local": config_path}).resolve("local")

	assert profile.model_services[0].model == "gpt-5.4-mini"
	assert profile.model_services[0].response_mode == "chat-completions"
