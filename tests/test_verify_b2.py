from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.storage.base import StorageOperationError, StorageVerificationResult
from scripts import verify_b2


class FakeSettings:
    log_level = "INFO"
    secret_values: tuple[str, ...] = ()

    def validate_service_configuration(self) -> None:
        return None


class FakeStorage:
    def __init__(
        self,
        *,
        result: StorageVerificationResult | None = None,
        error: Exception | None = None,
        configured: bool = True,
    ) -> None:
        self.result = result
        self.error = error
        self._configured = configured
        self.cleanup_requests: list[bool] = []
        self.closed = False

    @property
    def configured(self) -> bool:
        return self._configured

    async def verify_upload_download(
        self,
        *,
        cleanup: bool = True,
        timeout: float | None = None,
    ) -> StorageVerificationResult:
        del timeout
        self.cleanup_requests.append(cleanup)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    async def close(self) -> None:
        self.closed = True


def install_fakes(monkeypatch: pytest.MonkeyPatch, storage: FakeStorage) -> None:
    monkeypatch.setattr(
        verify_b2,
        "Settings",
        SimpleNamespace(from_env=staticmethod(lambda: FakeSettings())),
    )
    monkeypatch.setattr(verify_b2, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_b2, "create_storage", lambda _settings: storage)


def verification_result(
    *,
    cleaned_up: bool,
    cleanup_error: str | None = None,
) -> StorageVerificationResult:
    return StorageVerificationResult(
        object_key="healthchecks/test-object.txt",
        size_bytes=31,
        cleaned_up=cleaned_up,
        cleanup_requested=True,
        cleanup_error=cleanup_error,
    )


async def test_verify_reports_complete_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FakeStorage(result=verification_result(cleaned_up=True))
    install_fakes(monkeypatch, storage)

    exit_code = await verify_b2._verify()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert storage.cleanup_requests == [True]
    assert storage.closed is True
    assert "verification complete" in captured.out
    assert "cleaned_up=True" in captured.out
    assert captured.err == ""


async def test_verify_reports_incomplete_cleanup_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = FakeStorage(
        result=verification_result(
            cleaned_up=False,
            cleanup_error="StorageOperationError",
        )
    )
    install_fakes(monkeypatch, storage)

    exit_code = await verify_b2._verify()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert storage.closed is True
    assert "verification incomplete" in captured.out
    assert "cleanup failed" in captured.err
    assert "StorageOperationError" in captured.err


async def test_verify_returns_configuration_exit_code_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage(configured=False)
    install_fakes(monkeypatch, storage)

    exit_code = await verify_b2._verify()

    assert exit_code == 2
    assert storage.closed is False


async def test_verify_propagates_storage_errors_but_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage(error=StorageOperationError("upload failed"))
    install_fakes(monkeypatch, storage)

    with pytest.raises(StorageOperationError):
        await verify_b2._verify()

    assert storage.closed is True


def test_main_exits_nonzero_when_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeStorage(
        result=verification_result(
            cleaned_up=False,
            cleanup_error="StorageOperationError",
        )
    )
    install_fakes(monkeypatch, storage)

    with pytest.raises(SystemExit) as exc_info:
        verify_b2.main()

    assert exc_info.value.code == 1


def test_main_exits_zero_when_verification_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = FakeStorage(result=verification_result(cleaned_up=True))
    install_fakes(monkeypatch, storage)

    with pytest.raises(SystemExit) as exc_info:
        verify_b2.main()

    assert exc_info.value.code == 0
