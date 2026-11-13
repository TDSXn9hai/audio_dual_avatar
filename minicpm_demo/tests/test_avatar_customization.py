import asyncio
import io
import subprocess
from pathlib import Path

import pytest
from starlette.datastructures import Headers, UploadFile

from gateway_modules.avatar_customization import (
    AvatarCustomizationError,
    AvatarCustomizationService,
    _terminate_process_tree,
)


def _upload(data: bytes, filename: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers=Headers({}),
    )


def test_create_job_streams_files_and_enforces_owner(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gateway_modules.avatar_customization._validate_media",
        lambda *args: None,
    )
    service = AvatarCustomizationService(
        project_root=tmp_path,
        data_root=tmp_path / "runtime",
        max_video_seconds=20,
        max_audio_seconds=20,
    )

    async def scenario():
        job = await service.create_job(
            owner_id="user-a",
            video=_upload(b"video", "face.mp4"),
            audio=_upload(b"audio", "voice.wav"),
            prompt="你是一名讲解员",
            name="我的形象",
            description="测试",
        )
        assert job.status == "queued"
        assert Path(job.video_path).read_bytes() == b"video"
        assert Path(job.audio_path).read_bytes() == b"audio"
        with pytest.raises(AvatarCustomizationError) as exc:
            await service.get_job(job.id, "other-user")
        assert exc.value.code == "job_not_found"
        loaded = await service.get_job(job.id, "user-a")
        assert loaded.display_name == "我的形象"
        cancelled = await service.cancel_job(job.id, "user-a")
        assert cancelled.status == "cancelled"

    asyncio.run(scenario())


def test_upload_size_is_enforced(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gateway_modules.avatar_customization._validate_media",
        lambda *args: None,
    )
    service = AvatarCustomizationService(
        project_root=tmp_path,
        data_root=tmp_path / "runtime",
        max_video_bytes=4,
    )

    async def scenario():
        with pytest.raises(AvatarCustomizationError) as exc:
            await service.create_job(
                owner_id="user-a",
                video=_upload(b"12345", "face.mp4"),
                audio=_upload(b"audio", "voice.wav"),
                prompt="prompt",
                name="name",
                description="",
            )
        assert exc.value.status_code == 413

    asyncio.run(scenario())


def test_default_media_limit_is_twenty_seconds(tmp_path: Path) -> None:
    service = AvatarCustomizationService(
        project_root=tmp_path,
        data_root=tmp_path / "runtime",
    )
    assert service.max_video_seconds == 20
    assert service.max_audio_seconds == 20


def test_owner_id_is_validated_before_upload(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gateway_modules.avatar_customization._validate_media",
        lambda *args: None,
    )
    service = AvatarCustomizationService(
        project_root=tmp_path,
        data_root=tmp_path / "runtime",
    )

    async def scenario():
        with pytest.raises(AvatarCustomizationError) as exc:
            await service.create_job(
                owner_id="../escape",
                video=_upload(b"video", "face.mp4"),
                audio=_upload(b"audio", "voice.wav"),
                prompt="prompt",
                name="name",
                description="",
            )
        assert exc.value.code == "invalid_owner_id"
        assert not list((tmp_path / "runtime" / "uploads").iterdir())

    asyncio.run(scenario())


def test_cancel_during_idle_check_prevents_subprocess(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gateway_modules.avatar_customization._validate_media",
        lambda *args: None,
    )
    service = AvatarCustomizationService(
        project_root=tmp_path,
        data_root=tmp_path / "runtime",
    )
    launched = False
    idle_started = asyncio.Event()
    idle_release = asyncio.Event()

    async def ensure_idle():
        idle_started.set()
        await idle_release.wait()

    async def create_process(*args, **kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("cancelled job must not launch add_avatar.sh")

    async def scenario():
        job = await service.create_job(
            owner_id="user-a",
            video=_upload(b"video", "face.mp4"),
            audio=_upload(b"audio", "voice.wav"),
            prompt="prompt",
            name="name",
            description="",
        )
        service._ensure_avatar_idle = ensure_idle
        task = asyncio.create_task(service._run_job(job.id))
        await idle_started.wait()
        await service.cancel_job(job.id, "user-a")
        idle_release.set()
        await task
        assert launched is False
        assert not Path(job.video_path).parent.exists()

    monkeypatch.setattr(
        "gateway_modules.avatar_customization.asyncio.create_subprocess_exec",
        create_process,
    )
    asyncio.run(scenario())


def test_terminate_process_tree_kills_shell_and_python_child() -> None:
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            "python -c 'import time; time.sleep(30)'; echo child-finished",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.sleep(0.5)
        child_pids = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(process.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        assert child_pids

        await _terminate_process_tree(process, timeout=3)
        await asyncio.sleep(0.2)

        assert process.returncode is not None
        for child_pid in child_pids:
            assert not Path(f"/proc/{child_pid}").exists()

    asyncio.run(scenario())


def test_cancel_job_stops_avatar_subprocess_tree(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "gateway_modules.avatar_customization._validate_media",
        lambda *args: None,
    )
    script = tmp_path / "add_avatar.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo '==> [3/6] Generating cache'\n"
        "python -c 'import time; time.sleep(30)'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    service = AvatarCustomizationService(
        project_root=tmp_path,
        data_root=tmp_path / "runtime",
    )

    async def scenario():
        job = await service.create_job(
            owner_id="user-a",
            video=_upload(b"video", "face.mp4"),
            audio=_upload(b"audio", "voice.wav"),
            prompt="prompt",
            name="name",
            description="",
        )
        task = asyncio.create_task(service._run_job(job.id))
        process = None
        for _ in range(50):
            process = service._processes.get(job.id)
            if process is not None:
                break
            await asyncio.sleep(0.1)
        assert process is not None
        await asyncio.sleep(0.3)
        child_pids = subprocess.run(
            ["ps", "-o", "pid=", "--ppid", str(process.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        assert child_pids

        await service.cancel_job(job.id, "user-a")
        await task
        await asyncio.sleep(0.2)

        assert not Path(f"/proc/{process.pid}").exists()
        for child_pid in child_pids:
            assert not Path(f"/proc/{child_pid}").exists()

    asyncio.run(scenario())
