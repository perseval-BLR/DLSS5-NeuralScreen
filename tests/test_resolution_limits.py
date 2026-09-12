"""Compounded downscale safety, portrait/odd/small sources and cap precedence."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resolution_limits import safe_processing_size
from settings_io import _work_size

assert safe_processing_size(640, 360, 64, 64) == (256, 144)
assert safe_processing_size(960, 540, 240, 136) == (256, 144)
assert safe_processing_size(480, 270, 240, 136) == (256, 144)
assert safe_processing_size(640, 360, 320, 180) == (320, 180)
assert safe_processing_size(100, 80, 64, 64) == (100, 80)
assert safe_processing_size(539, 901, 64, 64) == (256, 428)
assert _work_size(2560, 1440, .01) == (256, 144)
assert _work_size(901, 539, 1) == (901, 539)
for width, height in ((2560,1440),(3840,2160),(1920,1080),(1080,1920),(100,80),(20000,100)):
    w,h = _work_size(width,height,.1)
    assert 0<w<=min(width,2560) and 0<h<=min(height,1440)
print('PASS: safe combined resolution floor, aspect and bounds')
