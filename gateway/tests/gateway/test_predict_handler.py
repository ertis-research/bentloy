import unittest

import httpx

from gateway.app.handlers.predict_handler import PredictHandler


class _FakeResolver:
    def __init__(self):
        self.raise_value_error = False
        self.raise_runtime_error = False

    def resolve_predict_target(self, model_name: str, model_url: str = ""):
        if self.raise_value_error:
            raise ValueError("invalid model_url")
        if self.raise_runtime_error:
            raise RuntimeError("network down")
        return f"http://model-{model_name}:3000/predict", f"http://model-{model_name}:3000", None


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakePredictClient:
    def __init__(self):
        self.responses: list[_FakeResponse] = []
        self.raise_timeout = False

    async def __aenter__(self) -> "_FakePredictClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, json: dict):
        if self.raise_timeout:
            raise httpx.TimeoutException("timeout")
        if self.responses:
            return self.responses.pop(0)
        return _FakeResponse({"ok": True, "url": url, "payload": json})


class PredictHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_predict_success_updates_recent_model(self) -> None:
        resolver = _FakeResolver()
        recently_routed_models: dict[str, str] = {}
        fake_client = _FakePredictClient()
        handler = PredictHandler(
            target_resolver=resolver,  # type: ignore[arg-type]
            model_urls={"tiny_demo_model": "http://model-tiny-demo-model:3000"},
            recently_routed_models=recently_routed_models,
            client_factory=lambda timeout: fake_client,
        )

        result = await handler.predict(
            model_name="tiny_demo_model",
            data={"x": 1},
        )

        self.assertEqual(result["ok"], True)
        self.assertIn("tiny_demo_model", recently_routed_models)
        self.assertEqual(
            recently_routed_models["tiny_demo_model"],
            "http://model-tiny_demo_model:3000",
        )

    async def test_predict_returns_validation_error(self) -> None:
        resolver = _FakeResolver()
        resolver.raise_value_error = True
        handler = PredictHandler(
            target_resolver=resolver,  # type: ignore[arg-type]
            model_urls={},
            recently_routed_models={},
            client_factory=lambda timeout: _FakePredictClient(),
        )

        result = await handler.predict(
            model_name="tiny_demo_model",
            data={},
            model_url="bad-url",
        )

        self.assertEqual(result["error"], "invalid model_url")
        self.assertEqual(result["model"], "tiny_demo_model")
        self.assertIn("model_url", result["hint"])

    async def test_predict_returns_timeout_error(self) -> None:
        resolver = _FakeResolver()
        fake_client = _FakePredictClient()
        fake_client.raise_timeout = True
        handler = PredictHandler(
            target_resolver=resolver,  # type: ignore[arg-type]
            model_urls={},
            recently_routed_models={},
            client_factory=lambda timeout: fake_client,
        )

        result = await handler.predict(
            model_name="tiny_demo_model",
            data={},
        )

        self.assertEqual(result, {"error": "Timeout: tiny_demo_model"})

    async def test_batch_predict_mixed(self) -> None:
        resolver = _FakeResolver()
        fake_client = _FakePredictClient()
        fake_client.responses = [_FakeResponse({"prediction": 0.1})]
        handler = PredictHandler(
            target_resolver=resolver,  # type: ignore[arg-type]
            model_urls={},
            recently_routed_models={},
            client_factory=lambda timeout: fake_client,
        )

        result = await handler.batch_predict(
            [
                {"model_name": "tiny_demo_model", "data": {"x": 1}},
                {"data": {"x": 2}},
            ]
        )

        self.assertEqual(result["results"][0], {"prediction": 0.1})
        self.assertEqual(result["results"][1], {"error": "model_name is required"})


if __name__ == "__main__":
    unittest.main()
