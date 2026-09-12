from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(slots=True)
class GuideFrame:
    motion: np.ndarray
    reset: bool
    scene_score: float


class TemporalGuideGenerator:
    """Estimate the guide buffers an encoded video does not contain."""

    def __init__(self, width: int, height: int, flow_width: int = 320,
                 emit_small: bool = False) -> None:
        """width/height is the NGX work resolution.

        emit_small=True: hand back the motion field at the optical-flow
        resolution (~320x180) instead of the work resolution. The worker then
        upscales it on the GPU and the CPU is spared ~8 ms per frame — a
        resize and a conversion of 6 million values. The vectors are in the
        same units either way (work-resolution pixels); only the grid changes.
        """
        self.width = width
        self.height = height
        scale = min(1.0, flow_width / width)
        self.flow_width = max(64, int(round(width * scale / 2) * 2))
        self.flow_height = max(64, int(round(height * scale / 2) * 2))
        self.emit_small = emit_small
        self.previous_gray: np.ndarray | None = None
        self._zero_motion = np.zeros((self.height, self.width, 2), dtype=np.float16)
        self._zero_small = np.zeros((self.flow_height, self.flow_width, 2), dtype=np.float16)
        self._flow_f16 = np.empty((self.flow_height, self.flow_width, 2), dtype=np.float16)
        # Buffers for upscaling the motion field. Each frame is 3M pixels by
        # 2 channels: without reuse it cost ~11.6 ms in allocation, multiplying
        # the already-upscaled field and astype (measured, _work/bench_guides.py).
        self._flow_scaled = np.empty((self.flow_height, self.flow_width, 2), dtype=np.float32)
        self._motion_f32 = np.empty((self.height, self.width, 2), dtype=np.float32)
        self._motion_f16 = np.empty((self.height, self.width, 2), dtype=np.float16)
        self.dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        self.dis.setUseSpatialPropagation(True)
        self.dis.setFinestScale(1)
        # MV validation (Feeder 0.14 static-hypothesis pattern): DIS on a
        # static desktop produces small noise vectors (capture noise,
        # cursor jitter, UI shimmer). Vectors below the noise floor are
        # zeroed - NGX would otherwise treat them as real motion and smear
        # text/UI. The floor is in work-resolution pixels. Applying it on
        # the small flow grid erased real 1–6 pixel scrolling at high resolution.
        self._flow_noise_floor = 0.5

    @property
    def motion_width(self) -> int:
        """Width of the motion field process() hands back."""
        return self.flow_width if self.emit_small else self.width

    @property
    def motion_height(self) -> int:
        return self.flow_height if self.emit_small else self.height

    def _small_gray(self, rgba: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(rgba, cv2.COLOR_RGBA2GRAY)
        return cv2.resize(gray, (self.flow_width, self.flow_height), interpolation=cv2.INTER_AREA)

    def zero_guide(self) -> GuideFrame:
        """Fallback for a persistent process() failure: zero motion, reset=True.

        The frame keeps going to the worker (the picture does not freeze); NGX
        gets a zero motion field instead of a fresh one.
        """
        motion = self._zero_small if self.emit_small else self._zero_motion
        return GuideFrame(motion=motion, reset=True, scene_score=1.0)

    def process(self, rgba: np.ndarray | None = None,
                gray: np.ndarray | None = None) -> GuideFrame:
        """Compute the guides: motion/reset/scene_score.

        Either rgba (full-res BGR/RGBA — downsampled here) or a ready gray
        frame (flow-sized, uint8 2D) coming from the worker's reverse channel
        (GRAY/DDA). Gray wins: it is already the right size.
        """
        if gray is not None:
            current = gray.reshape(self.flow_height, self.flow_width).astype(np.uint8)
            if current.shape != (self.flow_height, self.flow_width):
                raise ValueError(
                    f"gray {current.shape} != expected {(self.flow_height, self.flow_width)}")
        else:
            current = self._small_gray(rgba)
        pixels = self.width * self.height
        if self.previous_gray is None:
            motion = self._zero_small if self.emit_small else self._zero_motion
            reset = True
            scene_score = 1.0
        else:
            scene_score = float(np.mean(cv2.absdiff(current, self.previous_gray))) / 255.0
            reset = scene_score > 0.24
            if reset or scene_score < 0.001:
                # Reset (scene cut) or static screen (desktop/text): no flow needed.
                # 0.001: above capture noise (~0.0002 @ +-2 LSB) and static 0.0,
                # below real motion: 2px scroll 0.03, 2px shift 0.002, fast cursor 0.0013.
                motion = self._zero_small if self.emit_small else self._zero_motion
            else:
                # NGX consumes current-to-previous motion in pixel units.
                # Only the forward flow is sent. A backward flow with a
                # forward/backward consistency + residual mask used to be
                # computed here, but the mask was never applied to `motion` —
                # it cost a second DIS pass, two remaps and a dilate per frame
                # and was thrown away. Removed; reinstate it only together
                # with the code that actually masks the motion vectors.
                cur_to_prev = self.dis.calc(current, self.previous_gray, None)
                # MV validation: zero the noise floor. DIS reports small
                # vectors even on a static screen (capture noise, cursor
                # jitter); NGX would smear text/UI on them. The mask is
                # computed on the flow grid (115k elements, not 3M).
                # Scale BEFORE the upscale: 115k elements instead of 3M, and
                # exactly equivalent because resize is linear (verified: the
                # two orders differ by 0.002, i.e. float16 rounding).
                np.multiply(cur_to_prev[..., 0], self.width / self.flow_width,
                            out=self._flow_scaled[..., 0])
                np.multiply(cur_to_prev[..., 1], self.height / self.flow_height,
                            out=self._flow_scaled[..., 1])
                mag = np.hypot(self._flow_scaled[..., 0], self._flow_scaled[..., 1])
                self._flow_scaled[mag < self._flow_noise_floor] = 0.0
                if self.emit_small:
                    # The worker upscales it on the GPU — all that is left
                    # here is converting 115k values to float16.
                    np.copyto(self._flow_f16, self._flow_scaled, casting="same_kind")
                    motion = self._flow_f16
                else:
                    cv2.resize(self._flow_scaled, (self.width, self.height),
                               dst=self._motion_f32, interpolation=cv2.INTER_LINEAR)
                    np.copyto(self._motion_f16, self._motion_f32, casting="same_kind")
                    motion = self._motion_f16
        self.previous_gray = current
        expected = (self.flow_width * self.flow_height if self.emit_small else pixels)
        assert motion.size == expected * 2
        assert motion.dtype == np.float16 and motion.flags["C_CONTIGUOUS"]
        # INVARIANT: motion is a reused buffer (either _motion_f16 or a
        # cached zero field). The next process() call overwrites it, so the
        # consumer must copy the data before then. The main loop does exactly
        # that: send_frame copies the frame into shared memory immediately.
        return GuideFrame(
            motion=motion,
            reset=reset,
            scene_score=scene_score,
        )
