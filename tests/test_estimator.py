import json

import httpx
import pytest

from litellm_roi.connectors import SourceError
from litellm_roi.estimator import MAX_EVIDENCE_CHARS, Estimator
from litellm_roi.storage import Store


async def test_temperature_zero_unanchored_prompt_and_cache_invalidation(settings, pr, tmp_path):
    calls = []

    def handler(req):
        assert req.headers["authorization"] == "Bearer test-inference-secret"
        data = json.loads(req.content)
        calls.append(data)
        assert data["temperature"] == 0
        assert data["model"] == "test-estimator"
        assert "3 hours" not in data["messages"][0]["content"]
        assert json.loads(data["messages"][1]["content"])["files"][0]["patch"] == pr["files"][0]["patch"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"hours": 4.25, "reasoning": "Timezone conversion and regression verification."}'}}]})

    estimator = Estimator(settings, Store(tmp_path), httpx.MockTransport(handler))
    first = await estimator.estimate(pr)
    second = await estimator.estimate(pr)
    assert first["hours"] == 4.25 and not first["cached"]
    assert second["cached"] and len(calls) == 1
    pr["title"] = "Updated evidence"
    await estimator.estimate(pr)
    settings.estimator_prompt = "Estimate the engineering hours."
    await estimator.estimate(pr)
    await estimator.close()
    assert len(calls) == 3


@pytest.mark.parametrize("content", ['{"hours": -1, "reasoning": "x"}', '{"hours": NaN, "reasoning":"x"}',
    '{"hours": true, "reasoning":"x"}', '{"hours": "4", "reasoning":"x"}', '{"hours": 4}', 'not json'])
async def test_invalid_estimates_are_not_accepted(settings, pr, tmp_path, content):
    estimator = Estimator(settings, Store(tmp_path), httpx.MockTransport(lambda _: httpx.Response(200,
        json={"choices": [{"message": {"content": content}}]})))
    with pytest.raises(SourceError):
        await estimator.estimate(pr)
    await estimator.close()


async def test_incomplete_and_oversized_evidence_never_calls_model(settings, pr, tmp_path):
    def never(_):
        pytest.fail("Incomplete evidence was sent to model")
    estimator = Estimator(settings, Store(tmp_path), httpx.MockTransport(never))
    pr["incomplete_diff"] = True
    assert (await estimator.estimate(pr))["status"] == "needs_review"
    pr["incomplete_diff"] = False
    pr["body"] = "x" * MAX_EVIDENCE_CHARS
    assert (await estimator.estimate(pr))["status"] == "needs_review"
    await estimator.close()
