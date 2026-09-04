import pytest
from scripts import verify_phase13_runtime


class FailingAdminEngine:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.disposed = False

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls == 1:
            raise RuntimeError("original-create-failure")
        raise RuntimeError("cleanup-mask-failure")

    def dispose(self) -> None:
        self.disposed = True


class SuccessfulCreateConnection:
    def __enter__(self) -> "SuccessfulCreateConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


class CleanupFailingAdminEngine(FailingAdminEngine):
    def connect(self) -> SuccessfulCreateConnection:
        self.connect_calls += 1
        if self.connect_calls == 1:
            return SuccessfulCreateConnection()
        raise RuntimeError("cleanup-mask-failure")


class RecordingConnection(SuccessfulCreateConnection):
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def execute(self, statement: object, *_args: object, **_kwargs: object) -> None:
        self.statements.append(str(statement))


class RecordingAdminEngine(FailingAdminEngine):
    def __init__(self) -> None:
        super().__init__()
        self.statements: list[str] = []

    def connect(self) -> RecordingConnection:
        self.connect_calls += 1
        return RecordingConnection(self.statements)


class FakeReservation:
    def close(self) -> None:
        return None


class TrackingReservation(FakeReservation):
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class ProcessWithFailingTermination:
    stdout = None

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        raise RuntimeError("process-cleanup-failure")


class HealthyResponse:
    status_code = 200

    def json(self) -> dict[str, str]:
        return {"service": "company-second-brain-api", "status": "ok"}


class SocketWithInjectedFailure:
    def __init__(self, failure_stage: str) -> None:
        self.failure_stage = failure_stage
        self.close_calls = 0

    def bind(self, _address: tuple[str, int]) -> None:
        if self.failure_stage == "bind":
            raise RuntimeError("bind-failure")

    def getsockname(self) -> tuple[str, int]:
        if self.failure_stage == "getsockname":
            raise RuntimeError("getsockname-failure")
        return ("127.0.0.1", 51001)

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("failure_stage", ["bind", "getsockname"])
def test_port_reservation_helper_closes_socket_on_acquisition_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    reservation = SocketWithInjectedFailure(failure_stage)
    monkeypatch.setattr(
        verify_phase13_runtime.socket,
        "socket",
        lambda *_args, **_kwargs: reservation,
    )

    with pytest.raises(RuntimeError, match=f"{failure_stage}-failure"):
        verify_phase13_runtime.reserve_loopback_port()

    assert reservation.close_calls == 1


def test_database_create_failure_is_not_masked_by_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FailingAdminEngine()
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:password@localhost:5432/company_brain",
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )

    with pytest.raises(RuntimeError, match="original-create-failure"):
        verify_phase13_runtime.main()

    assert engine.connect_calls == 1
    assert engine.disposed is True


def test_runtime_failure_is_not_masked_by_database_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = CleanupFailingAdminEngine()
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:password@localhost:5432/company_brain",
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "run_checked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("original-runtime-failure")
        ),
    )

    with pytest.raises(RuntimeError, match="original-runtime-failure"):
        verify_phase13_runtime.main()

    assert engine.connect_calls == 2
    assert engine.disposed is True


def test_owned_database_cleanup_runs_after_process_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = RecordingAdminEngine()
    ports = iter((51001, 51002))
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:password@localhost:5432/company_brain",
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "run_checked",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "reserve_loopback_port",
        lambda: (FakeReservation(), next(ports)),
    )
    monkeypatch.setattr(
        verify_phase13_runtime.subprocess,
        "Popen",
        lambda *_args, **_kwargs: ProcessWithFailingTermination(),
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "wait_for_tcp",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        verify_phase13_runtime.httpx,
        "get",
        lambda *_args, **_kwargs: HealthyResponse(),
    )

    with pytest.raises(RuntimeError, match="process-cleanup-failure"):
        verify_phase13_runtime.main()

    assert engine.connect_calls == 2
    assert any("DROP DATABASE IF EXISTS" in statement for statement in engine.statements)
    assert engine.disposed is True


def test_first_port_reservation_is_closed_when_second_acquisition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = RecordingAdminEngine()
    first_reservation = TrackingReservation()
    acquisition_calls = 0

    def reserve_port() -> tuple[FakeReservation, int]:
        nonlocal acquisition_calls
        acquisition_calls += 1
        if acquisition_calls == 1:
            return first_reservation, 51001
        raise RuntimeError("second-port-reservation-failure")

    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:password@localhost:5432/company_brain",
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "create_engine",
        lambda *_args, **_kwargs: engine,
    )
    monkeypatch.setattr(
        verify_phase13_runtime,
        "run_checked",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(verify_phase13_runtime, "reserve_loopback_port", reserve_port)

    with pytest.raises(RuntimeError, match="second-port-reservation-failure"):
        verify_phase13_runtime.main()

    assert first_reservation.close_calls == 1
    assert engine.connect_calls == 2
    assert any("DROP DATABASE IF EXISTS" in statement for statement in engine.statements)
    assert engine.disposed is True
