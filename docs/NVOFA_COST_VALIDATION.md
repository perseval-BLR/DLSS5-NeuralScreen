# NVOFA cost validation

## Decision

Do not enable a global cost threshold from these results. Cost rejection reduces
spurious flow in some newly revealed strips, but misses low-cost errors on static
text and rejects correct window motion. No production mask is added by this work.
Threshold 8 below is an illustrative tradeoff, not a recommended setting.

The investigation also found two consecutive guide-processing blocks in
`experimental` at fc6af02. The second block ignored the selected hardware backend,
ran DIS again, and advanced guide history twice. The accompanying main-loop fix
prepares one capture, updates guides once, and preserves the bypass and NVOFA
CPU-skip decisions. It is independent of any cost threshold.

## Reproduction

Hardware: RTX 5080, NVIDIA driver 616.64, Windows build 26200, 2026-09-14.
Build with `cmd /c native\build-host.bat`, then run:

```
runtime\python.exe tests\experiment_nvofa_confidence.py --run
```

The experiment displays a 640x360 window, captures it through WGC, and processes
320x180 gray frames with the production NVOFA FAST backend (grid 4, 8-bit cost).
Its 71 pairs cover a textured window moving horizontally over text (17), a flat
window moving the other way (17), fast motion at 12 gray pixels/frame (11),
diagonal motion (17), and a static control (9). Other movement is 4 gray
pixels/frame horizontally and 2 vertically for the diagonal case.

Every captured frame must match the intended synthetic frame, including a
nearest-reference check to reject stale captures. WGC queues are drained before
advancing either estimator's history. All measured pairs had zero capture MAE.
Each sequence starts with a reset; its expanded GPU motion must be zero. Temporal
hints remain enabled thereafter. The CPU comparison uses the same captured pair
and current `TemporalGuideGenerator`, including its existing static-hypothesis
trust check. No scene-cut reset occurred in these 71 pairs.

Raw NVOFA motion is read back after the production GPU expansion, averaged over
2x2 output pixels and converted to gray-pixel units. Offline filtering zeroes a
vector if the maximum cost of its four bilinear grid neighbours exceeds the
threshold. Tested thresholds: 0, 1, 2, 4, 8, 16, 32, 64, 128, 255. The filter is
an offline experiment, not a shader implementation or a runtime timing result.

Moving-window and static-background endpoint error (EPE) have known geometric
ground truth. Two gray pixels around boundaries are excluded. Newly revealed
background has **no visible correspondence** in the prior frame: it is excluded
from EPE and measured separately as the fraction with motion above 0.25 gray
pixels. Zeroing a vector there does not itself prove that NR handles disocclusion
correctly; it is a conservative fallback to be evaluated in rendered output.

## Measurements

Mean per-frame EPE in gray pixels; lower is better. Columns are moving window /
static background. All thresholds and rejection metrics are recorded in
[nvofa-cost-results.json](nvofa-cost-results.json).

| Sequence | Raw NVOFA | Cost > 8 rejected | CPU DIS + trust |
| --- | ---: | ---: | ---: |
| Textured, horizontal | 0.045 / 1.414 | 0.049 / 1.413 | 0.028 / 0.015 |
| Flat, reverse | 0.114 / 1.296 | 0.136 / 1.294 | 3.209 / 0.039 |
| Fast, horizontal | 0.074 / 3.097 | 0.088 / 3.081 | 0.769 / 0.077 |
| Diagonal | 0.033 / 1.831 | 0.050 / 1.778 | 0.042 / 0.032 |
| Static | 0.000 / 0.237 | 0.000 / 0.237 | 0.000 / 0.000 |

Fraction of newly revealed pixels with nonzero motion:

| Sequence | Raw NVOFA | Cost > 8 rejected | CPU DIS + trust |
| --- | ---: | ---: | ---: |
| Textured, horizontal | 98.0% | 27.7% | 98.5% |
| Flat, reverse | 100.0% | 100.0% | 100.0% |
| Fast, horizontal | 99.9% | 25.8% | 98.2% |
| Diagonal | 99.2% | 44.8% | 100.0% |

An aggressive threshold of zero is not a solution either. In the textured
calibration sequence it increases moving-window EPE from 0.045 to 0.394 while
static-background EPE remains 1.138. In the fast holdout it raises moving EPE
from 0.074 to 1.456. Retaining only cost-zero flow therefore still misses errors.

CPU DIS is not a universal winner: flat-window motion is poorly observable and
its trust check rejects much of that movement. NVOFA estimates that window's
translation substantially better. Repeated text also admits ambiguous matches;
the known static background is useful for exposing those errors rather than
assuming a low matching cost means the vector is correct.

## Limits and next experiment

These are vector-level synthetic measurements on one GPU/driver at one gray
resolution, not game FPS or final NR/FG image-quality measurements. Neither an
optimal threshold nor a general confidence probability is established. The next
candidate is a static-hypothesis/photometric check alongside cost, evaluated on
the same holdouts and final rendered text before enabling it. The current CPU
trust path provides a useful comparison, including its flat-region failure.

The official [NVIDIA programming guide](https://docs.nvidia.com/video-technologies/optical-flow-sdk/nvofa-programming-guide/index.html)
defines higher cost as lower reliability and recommends the 8-bit format; it
does not prescribe a universal rejection threshold. The test leaves that question
open rather than promoting one GPU's calibration to a default.

Raw gray, flow, cost, expanded motion, worker logs and per-pair metrics are written
to a unique `_work/nvofa-confidence/<run-id>/` directory. They are local artifacts;
the compact aggregate JSON is committed. No capture readback is added to ordinary
application runs.
