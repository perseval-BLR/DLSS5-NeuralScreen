"""A slow scroll must survive the noise filter at every working resolution."""
from pathlib import Path
import sys
from unittest.mock import Mock
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from guides import TemporalGuideGenerator


def main():
    for width, height in ((640, 360), (2560, 1440), (1440, 2560)):
        guide = TemporalGuideGenerator(width, height, emit_small=True)
        shape = (guide.flow_height, guide.flow_width)
        flow = np.zeros((*shape, 2), np.float32)
        # One real pixel of horizontal motion; 0.1 pixel vertical noise elsewhere.
        flow[:, :shape[1] // 2, 0] = guide.flow_width / width
        flow[:, shape[1] // 2:, 1] = .1 * guide.flow_height / height
        guide.dis = Mock()
        guide.dis.calc.return_value = flow
        guide.process(gray=np.full(shape, 100, np.uint8))
        result = guide.process(gray=np.full(shape, 103, np.uint8))
        assert not result.reset
        assert np.allclose(result.motion[:, :shape[1] // 2, 0], 1)
        assert not np.any(result.motion[:, shape[1] // 2:])
        static = guide.process(gray=np.full(shape, 103, np.uint8))
        assert not np.any(static.motion)
    print('PASS: one-pixel scrolling preserved; subpixel noise and static motion rejected')


if __name__ == '__main__':
    main()
