# Experimental GPU motion estimation

This branch is based on upstream v1.8.0 and contains only GPU motion estimation
and its opt-in control. It requires neither Super Resolution nor Frame Generation.
No library updater or UI detection is included.

The checkbox defaults off. Switching restarts the worker; off uses CPU DIS.
On uses a three-level D3D12 Lucas-Kanade flow pyramid and expands the result
into the neural renderer's motion texture. The worker logs `[gpu-flow]` when
active. CPU capture paths retain CPU DIS. Gray readback still supplies scene-cut
and adaptive exposure logic; the ordinary v1.8 capture/protocol order is retained.
This is not a zero-copy pipeline. No additional NVIDIA library is needed.

## Known quality limitation

The algorithm is an experiment, not a replacement suitable for every scene.
On an RTX 5080 synthetic translation test, one flow-grid pixel per frame produces
about 0.0014 median endpoint error. A 15-pixel displacement can fail badly
(about 15 pixels of error in the original experiment). Fast motion can distort.
Keep this limitation visible when reviewing or enabling the checkbox.

Earlier combined-branch tests showed lower processing time on moving content
and no meaningful static-scene gain. Those numbers do not establish performance
of this standalone v1.8 branch. The user also reported an FPS improvement in
the combined preview. No FPS guarantee is made here.

## Validation and reproduction

- `cmd /c native\build-host.bat`
- `runtime/python.exe tests/test_gpu_motion_controls.py`
- `runtime/python.exe tests/experiment_gpu_flow.py --quality`
- `runtime/python.exe tests/experiment_gpu_flow.py`

The native build, UI on/off/persistence/Russian-text test, and CPU/GPU quality
runs completed on RTX 5080 on 2026-09-13. Quality runs report errors rather
than asserting that the known large-motion defect has been fixed.

The harness opens a synthetic WGC source and worker presentation window. It
uses the existing live protocol, without SR/FG or prepared-capture extensions.
GPU quality comparisons read the gray image after the worker acknowledges the
frame, matching the dumped flow to its captured image. CPU comparisons use the
gray pair that CPU DIS actually processed. Timing is host wall-clock time and
includes capture/queue waits, not pure GPU kernel duration. Raw rows and logs
are saved under `_work/gpu-flow-results/`. Close the application before running
GPU benchmarks to avoid contention; the harness leaves personal config alone.
