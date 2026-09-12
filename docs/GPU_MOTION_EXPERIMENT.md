# GPU motion experiment — 2026-09-13

Outcome: the tested GPU Lucas–Kanade prototype reduces processing time on
scrolling input, but fails the large-motion quality check. Do not replace DIS
in the normal application with this prototype.

## Scope and setup

- Branch: `experiment/gpu-motion`, based on combined build `76637fc`.
- RTX 5080, Windows 11, NVIDIA driver 32.0.16.1664; bundled Python 3.11.14.
- Same experimental worker executable for A and B, selected with
  `NS_GPU_FLOW_EXPERIMENT=0` / `1`. Default is off.
- CPU baseline: production OpenCV DIS FAST, four threads, 320x180 grid.
- GPU candidate: three-level 80x45 -> 160x90 -> 320x180 Lucas–Kanade,
  six iterations per level, 5x5 windows, bilinear expansion to NR resolution.
- WGC capture, real NR and GPU presentation at 2560x1440; HDR enabled;
  SR and UI protection disabled; FG off or 2x.
- Repeatable synthetic textured scrolling with text. Motion runs use ABBA
  order, 220 frames per run, first 40 excluded: 360 measured frames per arm
  per FG setting. Static controls use 180 measured frames per arm.
- Measured CPU wall time includes CAP1 capture preparation and acknowledgement,
  gray access/guide generation, frame submission, NR, optional FG and present
  acknowledgement. Source drawing and the application's Python menu/event loop
  are not included. These are worker-pipeline timings, not whole-app FPS.

## Measurements

Mean milliseconds per processed frame:

| Input | FG | CPU DIS | GPU LK | Time reduction |
|---|---|---:|---:|---:|
| Scrolling, all samples | off | 9.789 | 8.157 | 16.7% |
| Scrolling, all samples | 2x | 14.049 | 10.796 | 23.2% |
| Scrolling, changed captures only | off | 11.018 | 8.383 | 23.9% |
| Scrolling, changed captures only | 2x | 14.514 | 11.109 | 23.5% |
| Static, forced processing | off | 8.014 | 8.150 | -1.7% |
| Static, forced processing | 2x | 10.822 | 10.801 | ~0% |

WGC delivered duplicate images in some iterations. New/changed captures were
219/360 vs 188/360 without FG and 306/360 vs 240/360 with FG (CPU vs GPU).
The all-sample comparison therefore mixes different proportions of static and
moving frames. The changed-only comparison is included to expose that effect.
No increase in unique displayed frames per second is established by this test.
Do not convert inverse processing time into a claimed game FPS improvement.

The static control deliberately processes repeated captures (`skip_static=False`)
to measure the added work. It does not model the normal idle application's
skip-static behavior. CPU DIS already avoids flow on static images.

## Quality gate

A separate 1280x720 run saves GPU flow through readback; dumping is disabled
for all performance runs. Compare interior vectors against the actual captured
pair's integer translation, identified by an exhaustive image alignment search.
Errors below are in 320x180 flow-grid pixels, not display pixels.

| Case | CPU DIS median endpoint error | GPU LK median endpoint error |
|---|---:|---:|
| Small motion, median across frames | 0.01350 | 0.00142 |
| Large 15-pixel translation | 0.01375 | 15.16391 |

The GPU prototype estimates approximately 0.84 pixels instead of 15 on the
large translation. CPU DIS correctly estimates 15. This is a failed quality
gate, even though small translations are accurate. No equivalence on game
occlusions, particles, camera rotation or temporal NR/FG image quality is claimed.

## What remains on CPU

This isolates replacement of DIS; it is not a zero-readback pipeline.
Gray readback still supplies scene-cut detection, adaptive exposure and optional
UI detection. The wire still carries a placeholder motion payload for the GPU
arm. There is still a GPU fence wait at the motion stage. Eliminating those costs
is a separate experiment. GPU stage-only timestamps were not added here.

The application experiment also times `prepare_capture` separately; it was
outside the existing per-stage timers. This timing must not be treated as pure
copy time: capture availability and GPU queue waits are included.

## Reproduce

From the experiment worktree, with no other NeuralScreen instance active:

```powershell
cmd /c native\build-host.bat
runtime/python.exe tests/experiment_gpu_flow.py --quality
runtime/python.exe tests/experiment_gpu_flow.py
runtime/python.exe tests/experiment_gpu_flow.py --static
```

Runs briefly display a synthetic source and the worker's overlay, then close
them. They do not modify personal config. Raw timing rows, flow dumps, worker
logs and `summary.json` are in `_work/gpu-flow-results/`.

Next useful step: improve robustness to larger displacement (deeper pyramid,
coarse matching and confidence rejection), then rerun the same A/B timing and
quality checks. Extra quality work will consume some of the measured savings.
Main application and published PR branches were not changed by this experiment.

## Interactive preview

Branch `feature/gpu-motion-preview` adds a persisted `gpu_motion` checkbox to
the main menu. It defaults off. Switching restarts the worker; CPU DIS is used
when off. Start this worktree through its `NeuralScreen.vbs`. The GPU algorithm
is unchanged and still fails the large-motion quality gate described above.
