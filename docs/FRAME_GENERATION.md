# Experimental desktop frame generation

Off by default. Enable Frame Generation in the processing menu and select
an output multiplier from 2x to 4x. Requires `native/nvngx_dlssg.dll`, supplied
separately from the source repository.

Generated frames follow neural rendering and optional SR. A separate presenter
paces bounded frame slots and drops outdated generated frames. It still shares
the GPU command queue with neural rendering; this does not guarantee lower latency
or higher displayed FPS under GPU saturation.

HDR capture stays FP16 scRGB. The edited result is converted to HDR10 PQ for FG,
which does not accept scRGB. Ordinary HDR presentation continues to use scRGB.
Recording and Spout export real SDR frames, not generated frames.

This is experimental: depth is flat and motion is estimated from capture.
Occlusion and UI artifacts remain possible. HUD detection is a separate proposal.
Bypass and FG failures return to ordinary presentation.

Validation:
- `runtime/python.exe tests/test_framegen_controls.py`
- `runtime/python.exe tests/test_frame_generation.py --run`
- `runtime/python.exe tests/test_frame_generation.py --hdr --dynamic --sr --check-pixels`
- `native/test-hdr.bat`

Native tests require an NVIDIA GPU; HDR tests require an HDR-enabled display.
