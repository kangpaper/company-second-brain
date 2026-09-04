from typing import Any

from company_brain.ai.orchestrator import ProviderDraft
from company_brain.api.ai import DeterministicGroundedProvider, get_ai_provider
from company_brain.main import app

FAILURE_QUESTION = "Tổng quan khách hàng"
FAILURE_SECRET = "runtime-provider-secret-must-not-leak"


class RuntimeProbeProvider:
    provider_name = "runtime-probe"
    model_name = "runtime-probe-v1"

    def generate(self, *, question: str, context: dict[str, Any]) -> ProviderDraft:
        if question == FAILURE_QUESTION:
            raise RuntimeError(FAILURE_SECRET)
        return DeterministicGroundedProvider().generate(question=question, context=context)


def runtime_probe_provider_factory() -> type[RuntimeProbeProvider]:
    return RuntimeProbeProvider


app.dependency_overrides[get_ai_provider] = runtime_probe_provider_factory
