# Experimental HDR support

HDR desktop/window capture and scRGB presentation, contributed in PR #36. HDR is
detected automatically on the captured display. The NVIDIA neural runtime still
operates on an SDR proxy; this is **not native HDR inference**.

## Turning it on

**SETTINGS → CAPTURE → HDR compatibility.** Off by default: the mode changes the
capture format, the swap chain format and the colour space of the presentation,
and each of those is a way for the picture to fail on hardware we cannot test on.
The switch restarts the worker, which takes about a second.

Keep HDR enabled in Windows as well. Desktop capture and single-window capture
then select the HDR path automatically, and the log contains both:

```text
[hdr] capture=FP16 scRGB; neural processing=SDR proxy; export=SDR
[hdr] presentation=FP16 scRGB
```

The switch is carried to the worker as `NS_HDR`, read once at process start - the
same hand-off `NS_SPOUT` uses. `NS_HDR=1` in the environment is what the switch
writes; anything else, including an unset variable, is the original SDR capture
path. That path is not HDR-compatible: with Windows HDR on and this switch off,
the picture is captured in SDR and the menu says so.

If FP16 duplication is refused - no `IDXGIOutput5`, or the driver says no - the
capture falls back to SDR rather than failing, and the log says which of the two
happened.

## Behavior

- Desktop capture negotiates FP16 with `IDXGIOutput5::DuplicateOutput1` when HDR is
  enabled. Single-window WGC capture requests FP16 on an HDR display.
- The raw signed scRGB image is retained in the shared GPU texture. The new
  presentation texture and swap chain use `R16G16B16A16_FLOAT` and
  `RGB_FULL_G10_NONE_P709`; scRGB 1.0 represents 80 nits.
- The SDR content brightness setting is read for the actual captured display with
  `DisplayConfigGetDeviceInfo`, not inferred from registry entries or bit depth.
- The neural effect is applied as a bounded linear-light residual to the original
  HDR image. With identical proxy input/output, the original FP16 RGB values are
  preserved exactly, including negative values used for wide-gamut colors.
- NR OFF and the unprocessed side of the comparison wipe retain the raw HDR RGB
  values. The divider is rendered at the display's SDR white level.
- HDR/SDR mode changes are polled; the capture session is reopened when needed.
  Texture format changes trigger the existing capture rebuild path. The swap chain
  can switch between SDR and scRGB without changing the pipe protocol.

## Processing contract

Let `C` be the original linear scRGB RGB, `W` the Windows SDR white level in scRGB
units, and `P = max(0, C.r, C.g, C.b)`. The 8-bit neural input is:

```text
proxy = quantize8(linear_to_sRGB(max(C, 0) / (W + P)))
```

The existing resize, neural evaluation, and residual composite operate on this
proxy. The final HDR output uses the full-size quantized input `I` and result `O`:

```text
delta = clamp(sRGB_to_linear(O) - sRGB_to_linear(I), -0.25, 0.25)
HDR = clamp(C + (W + P) * delta, -65504, 65504)
```

This is an approximate way to transfer an SDR neural edit to HDR. It avoids the
singularity of inverse tone mapping near 1.0 and keeps untouched HDR detail in the
original. It does not promise the same artistic result as a neural model trained
and evaluated directly in HDR. Strong edits can change highlights and colors.

## Limits

- Built-in screenshots, video recording, and Spout remain **tone-mapped 8-bit SDR**.
  They contain the processed proxy, not an HDR copy of the screen.
- Python/pygame fallback capture and presentation remain SDR. The two `[hdr]` log
  lines above distinguish an active HDR path from a fallback.
- The upstream desktop worker still selects output 0 of its selected adapter.
  Multi-monitor selection, windows spanning mixed HDR/SDR displays, and cross-GPU
  capture are not certified by this change.
- Exclusive fullscreen remains subject to the upstream overlay limitations.
- Hardware smoke tests verify successful capture, processing, presentation, export,
  and recovery to SDR. They are not a calibrated measurement of monitor luminance
  or perceptual image quality. Windows HDR hot toggles, window moves across mixed
  displays, sleep/resume, and sustained performance need additional hardware testing.

## Validation

```bat
native\build-host.bat
runtime\python.exe tests\test_hdr_shaders.py
runtime\python.exe tests\test_hdr_switch.py
runtime\python.exe tests\test_bypass.py
runtime\python.exe tests\test_hdr_capture.py --run
runtime\python.exe tests\test_hdr_capture.py --desktop
```

All four are in `tests\run_tests.py` as well. `test_hdr_shaders.py` is
`native\test-hdr.bat` under a name the suite picks up; `test_hdr_capture.py`
with no argument asks Windows whether the primary display is in HDR and runs
the WGC variant when it is, rather than skipping in silence.

`test-hdr.bat` compiles and executes the **production HLSL** on WARP using GPU
readback. It covers non-multiple-of-8 dimensions, HDR highlight separation, signed
gamut preservation, bit-exact zero-edit and bypass, the comparison wipe, finite
bounded output, SDR output, and unchanged BGRA/RGBA channel order. No NVIDIA
runtime or HDR display is required for that test.

The Python integration tests require an RTX GPU, the NVIDIA runtime, and an HDR
primary display. They are opt-in because they briefly show an overlay. The WGC test
opens a colored 640x360 window; DDA uses the primary display. Both send bypass,
neural, and wipe frames, request SDR pixels, then disable capture and verify a
byte-exact SDR pipe frame after the swap-chain transition.

Observed on 2026-09-11: RTX 5080, HDR enabled, SDR white 280 nits, primary display
2560x1440; WGC and DDA each completed 12 capture/present frames plus one SDR
transition frame. Desktop neural processing ran at 1280x720. The existing SDR
bypass regression also passed (zero difference for bypass, nonzero neural edits
before and after it).

## References

- [Microsoft: HDR screen capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture)
- [Microsoft: DuplicateOutput1](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_5/nf-dxgi1_5-idxgioutput5-duplicateoutput1)
- [Microsoft: DirectX Advanced Color](https://learn.microsoft.com/en-us/windows/win32/direct3darticles/high-dynamic-range)
- [Microsoft: SDR white level](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-displayconfig_sdr_white_level)
