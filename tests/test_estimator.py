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
        assert "reasoning_effort" not in data
        assert "3 hours" not in data["messages"][0]["content"]
        assert "without AI assistance" in data["messages"][0]["content"]
        evidence = json.loads(data["messages"][1]["content"])
        assert "patch" not in evidence["files"][0]
        assert evidence["files"][0]["additions"] == 1
        assert evidence["changes"] == {"additions": 1, "deletions": 1, "files": 1, "commits": 1}
        assert evidence["commits"][0]["message"] == pr["commits"][0]["message"]
        assert "alice@example.com" not in data["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"hours": 4.25, "reasoning": "Timezone conversion and regression verification."}'}}]})

    estimator = Estimator(settings, Store(tmp_path), httpx.MockTransport(handler))
    first = await estimator.estimate(pr)
    second = await estimator.estimate(pr)
    assert first["hours"] == 4.25 and not first["cached"]
    assert first["effort_basis"] == "without_ai"
    assert second["cached"] and len(calls) == 1
    pr["title"] = "Updated evidence"
    await estimator.estimate(pr)
    settings.estimator_prompt = "Estimate the engineering hours."
    await estimator.estimate(pr)
    await estimator.close()
    assert len(calls) == 3


@pytest.mark.parametrize("model", ["gpt-6-luna", "openai/gpt-6-luna", "openrouter/openai/gpt-6-luna",
    "bedrock/global.openai.gpt-6-luna", "openai/gpt-6-sol"])
async def test_gpt6_preserves_temperature_zero_with_reasoning_disabled(settings, pr, tmp_path, model):
    settings.estimator_model = model

    def handler(req):
        data = json.loads(req.content)
        assert data["model"] == model
        assert data["temperature"] == 0 and data["reasoning_effort"] == "none"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"hours": 2, "reasoning": "Metadata-based estimate."}'}}]})

    estimator = Estimator(settings, Store(tmp_path), httpx.MockTransport(handler))
    try:
        assert (await estimator.estimate(pr))["hours"] == 2
    finally:
        await estimator.close()


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
    pr["incomplete_metadata"] = True
    assert (await estimator.estimate(pr))["status"] == "needs_review"
    pr["incomplete_metadata"] = False
    pr["body"] = "x" * MAX_EVIDENCE_CHARS
    assert (await estimator.estimate(pr))["status"] == "needs_review"
    await estimator.close()


async def test_large_and_missing_patches_do_not_block_metadata_estimate(settings, pr, tmp_path):
    pr["files"][0]["patch"] = "sensitive source code" * MAX_EVIDENCE_CHARS
    pr["files"].append({"filename": "diagram.png", "status": "added", "additions": 0, "deletions": 0})
    pr["changed_files"] = 2
    calls = []

    def handler(req):
        data = json.loads(req.content)
        evidence = json.loads(data["messages"][1]["content"])
        assert len(req.content) < 10000
        assert "sensitive source code" not in data["messages"][1]["content"]
        assert len(evidence["files"]) == 2
        calls.append(evidence)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"hours": 2, "reasoning": "Metadata-based estimate."}'}}]})

    estimator = Estimator(settings, Store(tmp_path), httpx.MockTransport(handler))
    first = await estimator.estimate(pr)
    assert first["status"] == "estimated" and first["evidence_source"] == "pr_metadata"
    pr["commits"][0]["message"] = "Revised scope description"
    await estimator.estimate(pr)
    assert len(calls) == 2
    await estimator.close()
