from argparse import Namespace
from types import SimpleNamespace

import numpy as np
import pytest

from muse_stream.inference.pipeline import InferencePipeline
from muse_stream.state import AppState


def test_generated_face_restored_before_bbox_resize_and_original_mask():
    source = np.full((16, 16, 3), 10, np.uint8)
    raw = np.full((256, 256, 3), 40, np.uint8)
    matrix = np.array([[1., 0, 0], [0, 1., 0]])
    calls = []

    class Restorer:
        def restore(self, face, alignment):
            calls.append((face.shape, alignment.copy()))
            return np.full_like(face, 80)

    state = AppState(args=Namespace())
    state.model.runtime = SimpleNamespace(face_restorer=Restorer())
    state.model.avatar = SimpleNamespace(
        coord_list_cycle=[(4, 4, 12, 12)],
        frame_list_cycle=[source],
        mask_list_cycle=[np.full((16, 16), 255, np.uint8)],
        mask_coords_list_cycle=[(0, 0, 16, 16)],
        restoration_matrices=np.array([matrix]),
    )
    output = InferencePipeline(state).blend_frame(0, raw)
    assert len(calls) == 1
    assert calls[0][0] == (256, 256, 3)
    np.testing.assert_array_equal(calls[0][1], matrix)
    assert np.all(output[4:12, 4:12] == 80)
    assert np.all(output[:4] == 10)
    assert np.all(raw == 40) and np.all(source == 10)


def test_restoration_error_does_not_silently_fall_back():
    class Broken:
        def restore(self, face, matrix):
            raise RuntimeError("GPEN inference failed")

    state = AppState(args=Namespace())
    state.model.runtime = SimpleNamespace(face_restorer=Broken())
    state.model.avatar = SimpleNamespace(
        coord_list_cycle=[(0, 0, 16, 16)],
        frame_list_cycle=[np.zeros((16, 16, 3), np.uint8)],
        restoration_matrices=np.zeros((1, 2, 3)),
    )
    with pytest.raises(RuntimeError, match="GPEN inference failed"):
        InferencePipeline(state).blend_frame(0, np.zeros((256, 256, 3), np.uint8))


def test_disabled_restoration_does_not_require_resources(monkeypatch):
    from muse_stream.inference.restoration import build_face_restorer
    monkeypatch.setenv("MUSETALK_FACE_RESTORATION", "off")
    monkeypatch.setenv("MUSETALK_GPEN_WEIGHTS", "/does/not/exist")
    assert build_face_restorer("cpu") is None


def test_enabled_restoration_rejects_missing_weights_before_model_import(monkeypatch):
    from muse_stream.inference.restoration import build_face_restorer
    monkeypatch.setenv("MUSETALK_FACE_RESTORATION", "gpen")
    monkeypatch.setenv("MUSETALK_GPEN_WEIGHTS", "/does/not/exist")
    with pytest.raises(FileNotFoundError, match="GPEN"):
        build_face_restorer("cpu")


def test_enabled_restoration_rejects_wrong_checkpoint(monkeypatch, tmp_path):
    from muse_stream.inference.restoration import build_face_restorer
    path = tmp_path / "wrong.pth"
    path.write_bytes(b"wrong model")
    monkeypatch.setenv("MUSETALK_FACE_RESTORATION", "gpen")
    monkeypatch.setenv("MUSETALK_GPEN_WEIGHTS", str(path))
    with pytest.raises(ValueError, match="checksum"):
        build_face_restorer("cpu")


def test_alignment_cache_rejects_stale_source_and_invalid_shapes(tmp_path):
    from muse_stream.inference.restoration import load_alignment_cache, source_signature
    (tmp_path / "coords.pkl").write_bytes(b"coordinates")
    (tmp_path / "full_imgs").mkdir()
    (tmp_path / "full_imgs/00000000.png").write_bytes(b"original frame")
    signature = source_signature(tmp_path)
    target = tmp_path / "gpen_alignment.npz"
    matrices = np.array([[[1., 0, 0], [0, 1., 0]]])
    inverse = np.array([[[1., 0, 0], [0, 1., 0]]])
    np.savez(target, matrices=matrices, inverse_matrices=inverse,
             signature=signature, face_size=256, version=2)
    assert load_alignment_cache(tmp_path, 1).shape == (1, 2, 3)
    with pytest.raises(ValueError, match="count|shape"):
        load_alignment_cache(tmp_path, 2)
    (tmp_path / "coords.pkl").write_bytes(b"new coordinates")
    with pytest.raises(ValueError, match="stale"):
        load_alignment_cache(tmp_path, 1)


def test_alignment_cache_rejects_nonfinite_values(tmp_path):
    from muse_stream.inference.restoration import load_alignment_cache, source_signature
    (tmp_path / "coords.pkl").write_bytes(b"coordinates")
    (tmp_path / "full_imgs").mkdir()
    (tmp_path / "full_imgs/00000000.png").write_bytes(b"original frame")
    np.savez(tmp_path / "gpen_alignment.npz",
             matrices=np.full((1, 2, 3), np.nan),
             inverse_matrices=np.zeros((1, 2, 3)),
             signature=source_signature(tmp_path), face_size=256, version=2)
    with pytest.raises(ValueError, match="finite"):
        load_alignment_cache(tmp_path, 1)


def test_split_restore_composite_matches_legacy_and_dumps_original_raw(monkeypatch, tmp_path):
    monkeypatch.setenv('MUSETALK_MASK_DUMP_DIR', str(tmp_path))
    import cv2
    class Restorer:
        calls = 0
        def restore(self, face, matrix):
            self.calls += 1
            return np.full_like(face, 80)
    restorer = Restorer()
    state = AppState(args=Namespace())
    source = np.full((16,16,3), 10, np.uint8)
    raw = np.full((256,256,3), 40, np.uint8)
    state.model.runtime = SimpleNamespace(face_restorer=restorer)
    state.model.avatar = SimpleNamespace(
        coord_list_cycle=[(4,4,12,12)], frame_list_cycle=[source],
        mask_list_cycle=[np.full((16,16),255,np.uint8)],
        mask_coords_list_cycle=[(0,0,16,16)],
        restoration_matrices=np.array([[[1.,0,0],[0,1.,0]]]))
    pipe = InferencePipeline(state)
    expected = pipe.blend_frame(0, raw)
    restored = pipe.restore_frame(1, raw)
    actual = pipe.composite_frame(1, restored)
    np.testing.assert_array_equal(actual, expected)
    assert restorer.calls == 2  # Exactly one invocation for each path.
    np.testing.assert_array_equal(cv2.imread(str(tmp_path/'generated_raw/frame_000001.png')), raw)
    assert np.all(source == 10) and np.all(raw == 40)


def test_direct_bbox_path_passes_bbox_sized_face_without_second_resize():
    class Restorer:
        def finalize_to_bbox(self, restored, original, inverse, bbox_size):
            assert bbox_size == (8, 8)
            return np.full((8, 8, 3), 80, np.uint8), np.zeros((8, 8, 3), np.uint8)

    state = AppState(args=Namespace())
    source = np.full((16, 16, 3), 10, np.uint8)
    state.model.runtime = SimpleNamespace(face_restorer=Restorer())
    state.model.avatar = SimpleNamespace(
        coord_list_cycle=[(4, 4, 12, 12)],
        frame_list_cycle=[source],
        mask_list_cycle=[np.full((16, 16), 255, np.uint8)],
        mask_coords_list_cycle=[(0, 0, 16, 16)],
    )
    pipe = InferencePipeline(state)
    result = pipe.finalize_restoration_to_bbox(
        0, np.zeros((512, 512, 3), np.uint8),
        np.zeros((256, 256, 3), np.uint8),
        np.array([[1., 0, 0], [0, 1., 0]]),
    )
    output = pipe.composite_frame(0, result, already_bbox=True)
    assert output.shape == source.shape
    assert np.all(output[4:12, 4:12] == 80)


def test_direct_bbox_matches_legacy_at_256_and_preserves_non_square_coordinates():
    import cv2
    import threading
    from muse_stream.inference.restoration import GPENRestorer
    restorer = object.__new__(GPENRestorer)
    restorer._lock = threading.Lock()
    restorer.support = np.ones((512, 512), np.float32)
    yy, xx = np.indices((512, 512))
    image = np.stack([xx % 256, yy % 256, (xx + yy) % 256], axis=-1).astype(np.uint8)
    original = np.zeros((256, 256, 3), np.uint8)
    inverse = np.array([[.5, .02, -2.], [-.01, .5, 3.]], np.float64)
    a = restorer.finalize(image, original, inverse)
    b, _ = restorer.finalize_to_bbox(image, original, inverse, (256, 256))
    np.testing.assert_array_equal(a, b)
    # A smooth ramp catches swapped row/column scaling and missing translation.
    image = np.stack([xx / 4, yy / 4, xx / 4], axis=-1).astype(np.uint8)
    inverse = np.array([[.5, 0, 4.], [0, .5, 6.]], np.float64)
    a = cv2.resize(restorer.finalize(image, original, inverse), (384, 320))
    b, _ = restorer.finalize_to_bbox(image, original, inverse, (384, 320))
    assert np.abs(a[30:-30, 30:-30].astype(float)-b[30:-30, 30:-30]).max() <= 1
