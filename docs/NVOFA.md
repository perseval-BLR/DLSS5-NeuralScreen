# Experimental NVIDIA Optical Flow motion backend

Select **Settings → Capture → Motion estimation → NVOFA (experimental)**.
Changing the selection restarts the worker. CPU DIS is the default. This is
motion estimation for neural rendering, independent of Super Resolution,
Frame Generation and the experimental GPU LK implementation.

The worker loads `nvofapi64.dll` from Windows System32, as installed by the
NVIDIA display driver. No new runtime DLL or network request is needed.
Availability is checked on the same D3D12 device used for neural rendering.
If initialization or execution fails, the worker logs the reason and returns
to CPU motion; the application displays a fallback notice. Select CPU and
then NVOFA to retry with a fresh worker.

For a manually launched worker, set `NS_MOTION_BACKEND=nvofa`. Unset or any
other value keeps CPU motion. The normal launcher derives this environment
variable from the saved `motion_backend` setting.

## Data path

Worker capture → existing small grayscale texture → two NVOFA input textures
→ hardware optical flow → GPU expansion into the neural motion texture
→ neural rendering → presentation.

The SDK 5.0 D3D12 interface uses a fast preset and a supported output grid
(4×4 when available). At 320×180 grayscale input that produces an 80×45
flow field. Signed S10.5 vectors are converted to floating point, expanded
bilinearly and scaled into work-resolution pixels. Input is the current
image, reference is the previous image: current-to-previous motion.
Reset frames use matching inputs, disable temporal hints and emit zero motion.

Graphics copy completion is an input fence for NVOFA. Its independent output
fence is waited on by the graphics queue before expansion. The SDR path keeps
the existing deferred submit/retirement behavior. Resize recreates the session;
shutdown and errors drain work and unregister resources before release.

Python stops running DIS only after the worker reports NVOFA active. It keeps
the existing grayscale history and scene-change detection so CPU fallback can
resume. The existing grayscale readback/shared-memory channel is still used;
this change does not remove it.

## Limits

The driver also performs preprocessing/postprocessing, and expansion uses the
graphics queue: this does not make motion estimation free. Static images can
cost more than CPU DIS's cheap static check; the existing "skip static frames"
setting still applies. No game-provided depth or motion vectors are available.

The driver produces an 8-bit cost texture, retained on the GPU. Higher cost
means a less reliable match; it is not a calibrated probability. This PR does
not apply a confidence mask: choosing a threshold needs separate validation
on occlusions, cuts and difficult motion. Synthetic translation accuracy does
not establish visual quality on those scenes or on every GPU/driver.

## Validation

Local measurements: RTX 5080, driver 616.64, Windows build 26200,
2560×1440 work/output, 320×180 flow, NR small enabled. The scrolling WGC
benchmark processes 220 requests per run and discards the first 40. Static
frame skipping is not requested by this harness. Times include guides,
worker capture/render/present and the reply, excluding source drawing.

| Test | CPU DIS mean | NVOFA mean |
| --- | ---: | ---: |
| HDR scrolling, final CPU/NVOFA/NVOFA/CPU sequence | 12.26 ms | 10.50 ms |
| HDR scrolling, earlier sequence | 11.78 ms | 10.41 ms |
| SDR scrolling, one pair | 11.32 ms | 9.78 ms |
| HDR static, one pair | 10.11 ms | 10.46 ms |

These runs suggest roughly 13–17% higher request throughput on this scrolling
fixture, not a game-FPS guarantee. Captures can repeat: 66–74% of measured
requests had a changed grayscale source in the final sequence. GPU load,
capture cadence and scene content affect the comparison. The static result
also shows why this is opt-in.

Synthetic translation checks passed for raw and expanded flow; median raw
error was approximately 0.02 grayscale pixels, including the 15-pixel jump.
Failure injection resumed CPU DIS without worker exit. Two live resizes and
normal worker shutdown passed. The application also ran with DDA/HDR and
NVOFA active for over 11,000 frames; that smoke test did not compare visual
quality or validate application hotkey shutdown.

Relevant existing checks passed: config/atomic persistence, translations,
module imports, settings hints, and frame retirement on WGC and DDA with
profiling on/off. The DDA pixel comparison initially failed twice and passed
on an isolated diagnostic rerun; clean `main` passed separately. This remains
an intermittent desktop-capture test result, not a clean full-suite claim.

Run from the repository root after `native\build-host.bat`:

```
runtime\python.exe tests\test_nvofa_controls.py
runtime\python.exe tests\test_settings_hints.py
runtime\python.exe tests\experiment_nvofa.py --quality
runtime\python.exe tests\experiment_nvofa.py --fallback
runtime\python.exe tests\experiment_nvofa.py --resize
runtime\python.exe tests\experiment_nvofa.py
runtime\python.exe tests\experiment_nvofa.py --static
runtime\python.exe tests\experiment_nvofa.py --sdr
```

The experiment explicitly requires a working NVIDIA optical-flow driver,
the program's NR runtime, and an interactive Windows desktop. It fails if
NVOFA silently falls back during a normal run. It opens a synthetic WGC source
without modifying user configuration. GPU tests should run sequentially.

`--quality` measures translation of the actually captured image pair and
checks both raw flow and the expanded neural motion texture, including a
15-pixel displacement at 320×180. `--fallback` injects an execution failure
after 60 frames and verifies DIS resumes. `--resize` changes work dimensions
twice in one process. Results and worker logs go to `_work/nvofa-results`.

Diagnostic-only `NS_NVOFA_DUMP=<existing directory>` reads back raw signed
flow, unsigned cost and half-float expanded motion as `*-NNNN.bin` files.
It synchronizes and writes files, so it must be off for timings.
`NS_NVOFA_TEST_FAIL_AT=<frame>` is the failure-injection hook used by the test.

API documentation: [NVIDIA Optical Flow programming guide](https://docs.nvidia.com/video-technologies/optical-flow-sdk/nvofa-programming-guide/index.html).
Header provenance and licenses: [native/include/nvofa/README.md](../native/include/nvofa/README.md).
