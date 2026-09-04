from company_brain.api.action_proposals import get_action_connector
from company_brain.main import app


class Phase12RuntimeConnector:
    def execute(
        self,
        *,
        operation: str,
        target: dict[str, object],
        parameters: dict[str, object],
        idempotency_key: str,
    ) -> dict[str, object]:
        if operation not in {"update_record", "delete_record"}:
            raise RuntimeError("unsupported runtime operation")
        if not idempotency_key:
            raise RuntimeError("missing idempotency key")
        del target, parameters
        return {"remote_reference": f"runtime:{idempotency_key}"}


app.dependency_overrides[get_action_connector] = Phase12RuntimeConnector
