from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import sys
import time

import numpy as np
import pytest


def client(mode='normal', timeout=2):
    from muse_stream.inference.gpen_process import _WorkerClient
    return _WorkerClient(
        [sys.executable, str(Path(__file__).with_name('gpen_worker_fixture.py')), '--mode', mode],
        startup_timeout=10, request_timeout=timeout,
    )


def test_cpu_processor_matches_existing_image_formula_without_inference_lock():
    from muse_stream.inference import restoration
    assert hasattr(restoration, 'GPENFaceProcessor'), 'Missing shared CPU processor'
    import cv2
    processor = restoration.GPENFaceProcessor()
    rng = np.random.default_rng(17)
    face = rng.integers(0, 256, (256, 256, 3), dtype=np.uint8)
    restored = rng.integers(0, 256, (512, 512, 3), dtype=np.uint8)
    inverse = np.array([[.49, .01, 3.], [-.01, .5, -2.]])
    support = np.zeros((512, 512), np.float32)
    support[20:492, 20:492] = 1
    support = cv2.GaussianBlur(support, (41, 41), 8)
    warped = cv2.warpAffine(restored, inverse, (256, 256), flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REFLECT_101)
    alpha = cv2.warpAffine(support, inverse, (256, 256), flags=cv2.INTER_LINEAR)[..., None]
    expected = np.clip(warped.astype(np.float32)*alpha + face*(1-alpha), 0, 255).astype(np.uint8)
    np.testing.assert_array_equal(processor.finalize(restored, face, inverse), expected)


def test_actual_child_roundtrip_keeps_images_and_requests_in_order():
    worker = client()
    try:
        assert worker.pid != os.getpid() and worker.alive
        for value in [2, 15, 40]:
            image = np.full((512, 512, 3), value, np.uint8)
            result = worker.infer(image)
            assert result['status']['pid'] == worker.pid
            np.testing.assert_array_equal(result['image'], np.full_like(image, value+1))
            assert result['model_ms'] >= 0
            assert np.all(image == value)
    finally:
        worker.close()
    assert not worker.alive
    worker.close()
    with pytest.raises(RuntimeError, match='closed'):
        worker.infer(np.zeros((512, 512, 3), np.uint8))


def test_concurrent_callers_cannot_consume_each_others_result():
    worker = client()
    try:
        with ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda v: worker.infer(np.full((512,512,3),v,np.uint8))['image'][0,0,0], [8,30,99]))
        assert results == [9,31,100]
    finally:
        worker.close()


@pytest.mark.parametrize('mode,match', [('error','fixture model failed'), ('exit','exited|closed|EOF')])
def test_child_failure_is_reported_and_reaped(mode, match):
    worker = client(mode)
    try:
        with pytest.raises(RuntimeError, match=match):
            worker.infer(np.zeros((512,512,3),np.uint8))
        assert worker.error and not worker.alive
    finally:
        worker.close()


@pytest.mark.parametrize('mode', ['slow','no_read','partial'])
def test_total_timeout_covers_compute_send_and_partial_receive(mode):
    worker = client(mode, timeout=.1)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match='timed out|timeout'):
            worker.infer(np.zeros((512,512,3),np.uint8))
        assert time.monotonic()-started < 3
        assert not worker.alive
    finally:
        worker.close()


def test_close_wakes_an_inflight_request():
    worker = client('slow', timeout=10)
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(worker.infer, np.zeros((512,512,3),np.uint8))
        time.sleep(.1)
        worker.close()
        with pytest.raises(RuntimeError):
            result.result(timeout=3)
    assert not worker.alive


def test_startup_failure_does_not_leave_live_child():
    with pytest.raises(RuntimeError, match='fixture startup failed'):
        client('startup_error')


def test_disabled_factory_never_creates_a_child(monkeypatch):
    from muse_stream.inference.restoration import build_face_restorer
    monkeypatch.setenv('MUSETALK_FACE_RESTORATION','off')
    monkeypatch.setenv('MUSETALK_GPEN_EXECUTION','invalid')
    assert build_face_restorer('cpu') is None


def test_factory_rejects_invalid_execution_before_loading_weights(monkeypatch):
    from muse_stream.inference.restoration import build_face_restorer
    monkeypatch.setenv('MUSETALK_FACE_RESTORATION','gpen')
    monkeypatch.setenv('MUSETALK_GPEN_EXECUTION','invalid')
    with pytest.raises(ValueError, match='EXECUTION'):
        build_face_restorer('cpu')


def test_parent_death_stops_worker_even_during_inference(tmp_path):
    muse = Path(__file__).resolve().parents[1]
    fixture = Path(__file__).with_name('gpen_worker_fixture.py')
    script = tmp_path/'parent.py'
    script.write_text(f'''import sys,os,time,threading
sys.path.insert(0,{str(muse)!r})
import numpy as np
from muse_stream.inference.gpen_process import _WorkerClient
worker=_WorkerClient([sys.executable,{str(fixture)!r},'--mode','hang'],startup_timeout=10,request_timeout=60)
threading.Thread(target=worker.infer,args=(np.zeros((512,512,3),np.uint8),),daemon=True).start()
time.sleep(.1)
print(worker.pid,flush=True)
os._exit(0)
''')
    result = subprocess.run([sys.executable,str(script)],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
    pid = int(result.stdout.strip())
    for _ in range(40):
        stat = Path(f'/proc/{pid}/stat')
        if not stat.exists() or stat.read_text().split()[2] == 'Z':
            break
        time.sleep(.05)
    else:
        os.kill(pid,9)
        pytest.fail('GPEN worker survived its parent')


def test_process_adapter_preserves_shape_and_reports_model_and_rpc_separately(monkeypatch):
    from muse_stream.inference import gpen_process
    real_client = gpen_process._WorkerClient
    def cpu_worker(command, **kwargs):
        return real_client([sys.executable,str(Path(__file__).with_name('gpen_worker_fixture.py'))], **kwargs)
    monkeypatch.setattr(gpen_process,'_WorkerClient',cpu_worker)
    restorer = gpen_process.GPENProcessRestorer(Path('unused'),'cpu')
    try:
        with pytest.raises(ValueError,match='512'):
            restorer.infer_aligned(np.zeros((256,256,3),np.uint8))
        assert restorer.status()['frames'] == 0
        image = restorer.infer_aligned(np.full((512,512,3),20,np.uint8))
        np.testing.assert_array_equal(image,np.full((512,512,3),21,np.uint8))
        model_ms = restorer.status()['last_ms']
        result = restorer.finalize(image,np.zeros((256,256,3),np.uint8),np.array([[.5,0,0],[0,.5,0]]))
        assert result.shape == (256,256,3)
        status = restorer.status()
        assert status['frames'] == 1 and status['execution'] == 'process'
        assert status['rpc_mean_ms'] >= status['mean_ms']
        assert status['last_ms'] == model_ms  # CPU finalize must not overwrite model timing.
        assert status['last_finalize_ms'] > 0
    finally:
        restorer.close()


def test_application_shutdown_reaps_child():
    from argparse import Namespace
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from muse_stream.api.app import create_app
    from muse_stream.state import AppState
    worker = client()
    state = AppState(args=Namespace())
    state.model.runtime = SimpleNamespace(face_restorer=worker)
    try:
        with TestClient(create_app(state)):
            assert worker.alive
        assert not worker.alive
        assert not state.service.ready and not state.service.accepting_streams
    finally:
        worker.close()


@pytest.mark.parametrize('during_load', [False,True])
def test_initialization_failure_or_shutdown_cleans_loaded_restorer(monkeypatch, during_load):
    from argparse import Namespace
    from types import SimpleNamespace
    from muse_stream import lifecycle
    from muse_stream.state import AppState
    worker = client()
    state = AppState(args=Namespace())
    def build(args):
        if during_load:
            lifecycle.shutdown(state)
        return SimpleNamespace(face_restorer=worker)
    def avatar(*args):
        raise RuntimeError('fixture invalid avatar cache')
    monkeypatch.setattr(lifecycle,'build_runtime',build)
    monkeypatch.setattr(lifecycle,'build_avatar',avatar)
    try:
        lifecycle.initialize(state)
        assert not worker.alive
        assert not state.service.ready
        if not during_load:
            assert 'fixture invalid avatar cache' in state.service.error
    finally:
        worker.close()


def test_status_marks_service_unready_when_worker_dies():
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from muse_stream.api.app import create_app
    from muse_stream.state import AppState
    worker = client()
    state = AppState()
    state.service.ready = True
    state.service.status = 'ready'
    state.model.runtime = SimpleNamespace(face_restorer=SimpleNamespace(
        status=lambda: {'mode':'gpen','execution':'process','alive':worker.alive,'error':worker.error},
        close=worker.close,
    ))
    try:
        worker._process.kill()
        worker._process.wait(timeout=2)
        with TestClient(create_app(state)) as http:
            status = http.get('/api/status').json()
            assert not status['ready']
            assert status['status'] == 'restoration_error'
            assert 'exited' in status['error']
    finally:
        worker.close()


def test_startup_timeout_reaps_unresponsive_child(monkeypatch):
    from muse_stream.inference.gpen_process import _WorkerClient
    real_popen = subprocess.Popen
    processes = []
    def start(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(subprocess,'Popen',start)
    with pytest.raises(RuntimeError, match='startup.*timed out'):
        _WorkerClient([sys.executable,str(Path(__file__).with_name('gpen_worker_fixture.py')),
                       '--mode','startup_slow'],startup_timeout=.5,request_timeout=2)
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.parametrize('value', ['0','-1','nan','inf'])
def test_invalid_timeout_is_rejected_before_spawning(monkeypatch, value):
    from muse_stream.inference.gpen_process import GPENProcessRestorer
    monkeypatch.setenv('MUSETALK_GPEN_REQUEST_TIMEOUT_SECONDS',value)
    with pytest.raises(ValueError,match='REQUEST_TIMEOUT_SECONDS'):
        GPENProcessRestorer(Path('unused'),'cpu')
