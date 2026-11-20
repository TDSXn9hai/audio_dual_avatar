"""Per-segment inference and frame blending."""

from __future__ import annotations

import time
import os
from pathlib import Path

import cv2
import numpy as np

from ..domain import LiveSegment
from ..log import log
from ..protocol import frame_pts_us
from ..state import AppState


def iter_speech_batches(
    whisper_chunks,
    latents,
    frame_indices: list[int],
    batch_size: int,
    device,
    whisper_offset: int = 0,
):
    """Yield fixed-size batches, padding the final partial batch.

    A trailing batch with a different shape can trigger a very slow cuDNN
    plan on some GPUs. Padding it to the regular batch size avoids that.
    """
    import torch

    for start in range(0, len(frame_indices), batch_size):
        end = min(start + batch_size, len(frame_indices))
        batch_indices = frame_indices[start:end]
        valid = end - start
        whisper_batch = torch.stack(
            [whisper_chunks[whisper_offset + i] for i in batch_indices]
        )
        latent_batch = torch.cat([latents[i] for i in batch_indices], dim=0)
        if valid < batch_size:
            repeats = batch_size - valid
            whisper_batch = torch.cat(
                [whisper_batch, whisper_batch[-1:].repeat(repeats, 1, 1)],
                dim=0,
            )
            latent_batch = torch.cat(
                [latent_batch, latent_batch[-1:].repeat(repeats, 1, 1, 1)],
                dim=0,
            )
        yield (
            batch_indices,
            whisper_batch.to(device),
            latent_batch.to(device),
        )


def contiguous_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    indices = np.flatnonzero(mask)
    if indices.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(indices) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks + 1, [indices.size]))
    return [
        (int(indices[start]), int(indices[end - 1]) + 1)
        for start, end in zip(starts, ends)
    ]


class InferencePipeline:
    """Generate the visible frames for one complete audio segment."""

    def __init__(self, state: AppState) -> None:
        self.state = state
        dump_dir = os.environ.get("MUSETALK_MASK_DUMP_DIR", "").strip()
        self.mask_dump_dir = Path(dump_dir) if dump_dir else None
        self._dumped_mask_keys: set[tuple[int, tuple[int, ...]]] = set()
        if self.mask_dump_dir is not None:
            self.mask_dump_dir.mkdir(parents=True, exist_ok=True)
            (self.mask_dump_dir / "generated_raw").mkdir(exist_ok=True)
            (self.mask_dump_dir / "generated_face").mkdir(exist_ok=True)
            (self.mask_dump_dir / "mask_crop").mkdir(exist_ok=True)
            (self.mask_dump_dir / "mask_full").mkdir(exist_ok=True)

    def iter_segment(self, segment: LiveSegment, frame_bases=None):
        """Yield timestamped frames, bypassing UNet for silent audio windows."""
        import torch

        state = self.state
        runtime = state.model.runtime
        assert runtime is not None and state.model.avatar is not None
        args = state.args
        assert args is not None

        (
            whisper_input_features,
            librosa_length,
        ) = runtime.audio_processor.get_audio_feature(
            str(segment.wav_path),
            weight_dtype=runtime.weight_dtype,
        )
        whisper_chunks = runtime.audio_processor.get_whisper_chunk(
            whisper_input_features,
            runtime.device,
            runtime.weight_dtype,
            runtime.whisper,
            librosa_length,
            fps=args.fps,
            audio_padding_length_left=args.audio_padding_length_left,
            audio_padding_length_right=args.audio_padding_length_right,
            feature_window_mode=args.feature_window_mode,
            feature_window_left=args.feature_window_left,
            feature_window_right=args.feature_window_right,
        )
        total_frames = segment.total_frames
        extra_frames = segment.extra_frames
        inference_frames = total_frames + extra_frames
        frame_offset = segment.frame_offset
        if segment.speech_mask is None:
            speech_mask = np.ones(inference_frames, dtype=bool)
        else:
            speech_mask = np.asarray(segment.speech_mask, dtype=bool)
            if speech_mask.size < inference_frames:
                speech_mask = np.pad(
                    speech_mask,
                    (0, inference_frames - speech_mask.size),
                    constant_values=False,
                )
            speech_mask = speech_mask[:inference_frames].copy()
        feature_frames = min(
            inference_frames,
            max(0, len(whisper_chunks) - frame_offset),
        )
        if feature_frames < inference_frames:
            speech_mask[feature_frames:] = False

        def source_at(offset):
            snapshot = (
                frame_bases[offset] if frame_bases is not None else segment.base_snapshot
            )
            frame_index = segment.global_start_frame + offset
            if snapshot is not None:
                return snapshot.bundle.avatar, snapshot.source_frame(frame_index)
            return state.model.avatar, frame_index

        def source_image(offset):
            avatar, source_index = source_at(offset)
            cycle = avatar.frame_list_cycle
            return cycle[source_index % len(cycle)]

        silent_frames = int(inference_frames - speech_mask.sum())
        generated_frames = int(speech_mask.sum())
        log(
            f"streaming {segment.wav_path.name}: frames={total_frames} "
            f"extra_frames={extra_frames} "
            f"generated={generated_frames} silent={silent_frames} "
            f"analysis_frames={segment.analysis_frames} "
            f"frame_offset={frame_offset} "
            f"feature_window={args.feature_window_mode} "
            f"start_pts={frame_pts_us(segment.global_start_frame, args.fps)}us"
        )

        cursor = 0
        batch_started = time.monotonic()
        with torch.no_grad():
            for run_start, run_end in contiguous_runs(speech_mask):
                while cursor < run_start:
                    frame_index = segment.global_start_frame + cursor
                    yield (
                        "silent",
                        frame_index,
                        source_image(cursor),
                    )
                    cursor += 1

                frame_indices = list(range(run_start, run_end))
                latents = {}
                for i in frame_indices:
                    avatar, source_index = source_at(i)
                    cycle = avatar.input_latent_list_cycle
                    latents[i] = cycle[source_index % len(cycle)]
                for (
                    batch_indices,
                    whisper_batch,
                    latent_batch,
                ) in iter_speech_batches(
                    whisper_chunks,
                    latents,
                    frame_indices,
                    args.batch_size,
                    runtime.device,
                    whisper_offset=frame_offset,
                ):
                    audio_feature_batch = runtime.pe(whisper_batch)
                    latent_batch = latent_batch.to(
                        device=runtime.device,
                        dtype=runtime.unet.model.dtype,
                    )
                    pred_latents = runtime.unet.model(
                        latent_batch,
                        runtime.timesteps,
                        encoder_hidden_states=audio_feature_batch,
                    ).sample
                    pred_latents = pred_latents.to(
                        device=runtime.device,
                        dtype=runtime.vae.vae.dtype,
                    )
                    recon = runtime.vae.decode_latents(pred_latents)
                    batch_seconds = time.monotonic() - batch_started
                    if batch_seconds > 2:
                        log(
                            f"slow inference batch: valid={len(batch_indices)} "
                            f"first={batch_indices[0]} dt={batch_seconds:.3f}s"
                        )
                    batch_started = time.monotonic()

                    for offset, res_frame in enumerate(
                        recon[: len(batch_indices)]
                    ):
                        frame_index = (
                            segment.global_start_frame + batch_indices[offset]
                        )
                        yield "generated", frame_index, res_frame
                cursor = run_end

            while cursor < inference_frames:
                frame_index = segment.global_start_frame + cursor
                yield (
                    "silent",
                    frame_index,
                    source_image(cursor),
                )
                cursor += 1

    def blend_frame(
        self,
        frame_index: int,
        res_frame: np.ndarray,
        snapshot=None,
    ) -> np.ndarray:
        """Compatibility path for codecs without separate restoration workers."""
        return self.composite_frame(
            frame_index,
            self.restore_frame(frame_index, res_frame, snapshot),
            snapshot=snapshot,
        )

    def restore_frame(
        self,
        frame_index: int,
        res_frame: np.ndarray,
        snapshot=None,
    ) -> np.ndarray:
        """Restore a generated crop, keeping the source frame untouched."""
        avatar = (
            snapshot.bundle.avatar
            if snapshot is not None
            else self.state.model.avatar
        )
        assert avatar is not None
        source_index = snapshot.source_frame(frame_index) if snapshot is not None else frame_index
        bbox = avatar.coord_list_cycle[
            source_index % len(avatar.coord_list_cycle)
        ]
        if tuple(bbox) == (0, 0, 0, 0):
            return res_frame
        self._dump_generated_raw(frame_index, res_frame)
        restorer = getattr(self.state.model.runtime, "face_restorer", None)
        if restorer is not None:
            matrices = avatar.restoration_matrices
            return restorer.restore(
                res_frame, matrices[source_index % len(matrices)])
        return res_frame

    def prepare_restoration(
        self,
        frame_index: int,
        res_frame: np.ndarray,
        snapshot=None,
    ):
        avatar = (
            snapshot.bundle.avatar
            if snapshot is not None
            else self.state.model.avatar
        )
        assert avatar is not None
        restorer = getattr(self.state.model.runtime, "face_restorer", None)
        if restorer is None:
            return res_frame, None, None
        source_index = snapshot.source_frame(frame_index) if snapshot is not None else frame_index
        matrix = avatar.restoration_matrices[
            source_index % len(avatar.restoration_matrices)]
        inverse = avatar.restoration_inverse_matrices[
            source_index % len(avatar.restoration_inverse_matrices)]
        aligned, inverse = restorer.prepare(res_frame, matrix, inverse)
        return aligned, res_frame, inverse

    def infer_restoration(self, aligned: np.ndarray) -> np.ndarray:
        restorer = getattr(self.state.model.runtime, "face_restorer", None)
        return aligned if restorer is None else restorer.infer_aligned(aligned)

    def finalize_restoration(
        self, restored: np.ndarray, original: np.ndarray, inverse: np.ndarray
    ) -> np.ndarray:
        restorer = getattr(self.state.model.runtime, "face_restorer", None)
        if restorer is None or original is None or inverse is None:
            return restored
        return restorer.finalize(restored, original, inverse)

    def finalize_restoration_to_bbox(
        self,
        frame_index: int,
        restored: np.ndarray,
        original: np.ndarray,
        inverse: np.ndarray,
        snapshot=None,
    ) -> np.ndarray:
        restorer = getattr(self.state.model.runtime, "face_restorer", None)
        if restorer is None or original is None or inverse is None:
            return restored
        avatar = (
            snapshot.bundle.avatar
            if snapshot is not None
            else self.state.model.avatar
        )
        source_index = snapshot.source_frame(frame_index) if snapshot is not None else frame_index
        bbox = avatar.coord_list_cycle[source_index % len(avatar.coord_list_cycle)]
        x1, y1, x2, y2 = (int(value) for value in bbox)
        result, _ = restorer.finalize_to_bbox(
            restored, original, inverse, (x2 - x1, y2 - y1))
        return result

    def composite_frame(
        self,
        frame_index: int,
        res_frame: np.ndarray,
        already_bbox: bool = False,
        snapshot=None,
    ) -> np.ndarray:
        """Paste an already restored crop with the original bbox and mask."""
        from musetalk.utils.blending import get_image_blending

        avatar = (
            snapshot.bundle.avatar
            if snapshot is not None
            else self.state.model.avatar
        )
        assert avatar is not None
        source_index = snapshot.source_frame(frame_index) if snapshot is not None else frame_index
        bbox = avatar.coord_list_cycle[
            source_index % len(avatar.coord_list_cycle)
        ]
        ori_frame = avatar.frame_list_cycle[
            source_index % len(avatar.frame_list_cycle)
        ]

        if tuple(bbox) == (0, 0, 0, 0):
            return ori_frame

        x1, y1, x2, y2 = bbox
        try:
            resized = (
                res_frame.astype(np.uint8)
                if already_bbox
                else cv2.resize(
                    res_frame.astype(np.uint8),
                    (x2 - x1, y2 - y1),
                )
            )
        except Exception:
            return ori_frame

        mask = avatar.mask_list_cycle[
            source_index % len(avatar.mask_list_cycle)
        ]
        mask_crop = avatar.mask_coords_list_cycle[
            source_index % len(avatar.mask_coords_list_cycle)
        ]
        self._dump_mask_debug(
            frame_index=frame_index,
            generated_face=resized,
            mask=mask,
            mask_crop=mask_crop,
            frame_shape=ori_frame.shape[:2],
        )
        return get_image_blending(
            ori_frame,
            resized,
            [x1, y1, x2, y2],
            mask,
            mask_crop,
        )

    def _dump_generated_raw(
        self,
        frame_index: int,
        generated_face: np.ndarray,
    ) -> None:
        if self.mask_dump_dir is None:
            return
        path = self.mask_dump_dir / "generated_raw" / f"frame_{frame_index:06d}.png"
        if not path.exists():
            cv2.imwrite(str(path), generated_face.astype(np.uint8))

    def _dump_mask_debug(
        self,
        frame_index: int,
        generated_face: np.ndarray,
        mask: np.ndarray,
        mask_crop: tuple[int, int, int, int],
        frame_shape: tuple[int, int],
    ) -> None:
        """Persist the exact alpha mask used by blending for visual diagnosis."""
        if self.mask_dump_dir is None:
            return
        key = (frame_index % 1000000, tuple(int(v) for v in mask_crop))
        if key in self._dumped_mask_keys:
            return
        self._dumped_mask_keys.add(key)
        stem = f"frame_{frame_index:06d}"
        cv2.imwrite(
            str(self.mask_dump_dir / "generated_face" / f"{stem}.png"),
            generated_face,
        )
        mask_array = np.asarray(mask)
        if mask_array.ndim == 3:
            mask_array = cv2.cvtColor(mask_array, cv2.COLOR_BGR2GRAY)
        cv2.imwrite(
            str(self.mask_dump_dir / "mask_crop" / f"{stem}.png"),
            mask_array,
        )
        full_mask = np.zeros(frame_shape, dtype=np.uint8)
        x1, y1, x2, y2 = (int(v) for v in mask_crop)
        h, w = full_mask.shape
        x1c, y1c = max(0, x1), max(0, y1)
        x2c, y2c = min(w, x2), min(h, y2)
        if x2c > x1c and y2c > y1c:
            crop = mask_array
            crop = crop[y1c - y1:y2c - y1, x1c - x1:x2c - x1]
            if crop.shape != (y2c - y1c, x2c - x1c):
                crop = cv2.resize(crop, (x2c - x1c, y2c - y1c))
            full_mask[y1c:y2c, x1c:x2c] = crop
        cv2.imwrite(
            str(self.mask_dump_dir / "mask_full" / f"{stem}.png"),
            full_mask,
        )
