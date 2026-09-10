# NeuralScreen internals

How it works, what was measured, and why the decisions went the way
they did. For installing and using the program see
[README.md](../README.md).

Every number here was measured on this machine - RTX 5070 Ti, driver
616.56, Windows 11 - and says so where it matters. Where an earlier
conclusion turned out to be wrong, the correction is kept rather than
quietly edited out: the mistakes are the useful part.

## One window: Windows Graphics Capture (WGCW)

`Num5` swaps the input from Desktop Duplication of the whole screen to
Windows Graphics Capture of one window. Both sources hand the worker the same
kind of `ID3D11Texture2D`, so everything downstream - the NT-shared texture,
the fence, the BGRA->RGBA swizzle, the luminance channel for the guides - is
the same code.

Why it matters, measured rather than assumed:

* A per-window capture is unaffected by what is drawn on top of the window. A
  fullscreen overlay covering the target contributes **0.0%** of the captured
  pixels while the window's own content is **100%**. No self-capture loop.
* So `WDA_EXCLUDEFROMCAPTURE` comes off in this mode, and that is the whole
  point: with it on, the NVIDIA App writes **no file at all** (0 out of 4
  attempts); in one-window mode it recorded 26.1 MB of the same desktop.
* The yellow "this window is being captured" border can be turned off.
  `GraphicsCaptureSession.IsBorderRequired = false` takes effect for an
  unpackaged process on Windows 11 26200 even though
  `GraphicsCaptureAccess.RequestAccessAsync(Borderless)` answers
  `UserPromptRequired`. Measured against a control run: 12 yellow pixels in
  the ring around the window with the capture on, 12 with it off.

Sizes are **physical pixels**. `WGCW` is acknowledged with the size the
capture really produces, and the pipeline is rebuilt for exactly that - a
window's logical size on a scaled display is a different number, and
`GetWindowRect` is a third one (it includes the invisible resize border). The
overlay is placed by `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`,
which is the rectangle the capture agrees with.

The capture is event-driven: a window that does not redraw produces one frame
and then nothing, exactly like `DXGI_ERROR_WAIT_TIMEOUT` on the duplication
path. The worker keeps the last frame.

A resize rebuilds the worker, the shared memory and every texture, so it waits
half a second for the new size to settle first. A move only moves the windows.

## Recording (Num0)

`Num0` starts/stops recording of the **NR-processed frame** into
`recordings/neuralscreen-<timestamp>.mp4`:

- AV1 NVENC hardware encoding at your desktop resolution, **60 fps**,
  quality-targeted VBR (`cq 16`, ~64 Mbps in practice, ceiling 250 Mbps),
  preset p6 + tune hq, sRGB/BT.709 color tags (metadata written both on the
  stream and on every frame — players render colors identical to the screen).
- The bitrate is a **ceiling, not a target**: on fast motion the encoder is
  allowed to spend more instead of dropping quality to hit a fixed number.
  Raising quality costs no encoding time — that is dominated by the colour
  conversion, not by the preset (measured: 1.6–1.7 s per 3 s of video at
  every setting tried).
- Recording runs at **60 fps** because the pipeline delivers ~55 frames per
  second. The previous 30 fps time base could not represent them: frames were
  squeezed into half as many ticks, which is what made fast motion fall apart
  regardless of bitrate.
- **Only the open menu is burned into the recording** — nothing else. Our
  own layer is hidden from external capture, so anything that must reach the
  file is drawn onto the frame before encoding. The same applies to
  screenshots. The HUD panel and the watermark used to be burned in as well;
  they are gone from the screen, and in a file they read as someone else's
  caption.
- Recording works in both NR ON and NR OFF (bypass) modes; the file duration
  matches real time (PTS is built from the wall clock).
- **System audio is recorded as a second track**: WASAPI loopback ("what you
  hear") from the default playback device, AAC 192 kbit/s stereo at the
  endpoint's own rate. No virtual cable, no microphone. Turn it off with
  `"record_audio": false` in `config.json`. A machine without a playback
  endpoint still records video — the sound is best-effort and never stops the
  recording.

  While nothing is playing at all, WASAPI loopback hands back no data rather
  than silence, so quiet stretches are padded from the same clock the video
  uses. Without that the audio track would simply be shorter than the video
  and everything after a pause would be out of sync.

### What recording costs, and why it is not the bitrate

Recording used to halve the frame rate. Measured at 4K with `NS_PHASE=1`,
per frame:

```
                    idle    recording   after both fixes
frame rate          55.7      20.6           30.1 FPS
recv                17.5      31.6           24.5 ms
encode (Python)      0.4      19.9            3.1 ms
worker frame        17.4      24.1           21.3 ms
```

Two costs, neither of them the bitrate:

- **19.9 ms of RGBA→yuv420p on the CPU** plus the nvenc submit, inside the
  capture loop. Encoding now runs in its own thread behind a 4-slot queue;
  the loop only computes the PTS and hands the frame over. On a full queue
  the frame is dropped rather than stalling the loop — the user is looking at
  the screen, not at the file, and a gap does not shift timing because the
  PTS comes from the clock.
- **~7 ms of pushing 33 MB down the pipe.** The `OUTS` channel hands the
  worker a named section to write pixels into instead. The copy out of the
  section happens on the reader thread; a copy is unavoidable because the
  section has one slot and the worker overwrites it next frame, while a
  recorded frame outlives that.

Lowering the bitrate does nothing for any of this: the time goes into the
colour conversion, not into the encoder. Which is why there is no bitrate
slider in the menu — it would be a knob that looks like it helps and does
not.

## config.json

| Field | Meaning |
|---|---|
| `monitor` | monitor index for capture |
| `width`, `height` | output resolution (**actual monitor resolution is used automatically when config is stale**) |
| `fullscreen` | borderless fullscreen window |
| `warmup` | NGX warmup frames at start |
| `work_scale` | 0.1–1.0, the resolution the network runs at, relative to the screen. Only has an effect with `nr_small` on |
| `nr_small` | process at a reduced resolution and compose the result onto the native frame: faster, sharp (the residual composite). Default `false` |
| `profile` | `Faithful`, `Natural`, `Strong / Cinematic`, `Extreme / Overdrive` |
| `intensity`, `local_tone`, `local_structure`, `skin_structure` | `null` = take from profile |
| `lang` | `ru` / `en` |
| `worker_present` | worker shows the frame in its own window (`false` — pygame output) |
| `motion_on_gpu` | worker upscales the motion field (`false` — CPU) |
| `capture_in_worker` | worker captures the desktop itself (DDA, `false` — dxcam in Python) |
| `pixels_in_shm` | result pixels come back through a shared section instead of the pipe (`false` — pipe, as before) |
| `split` | 0–1, share of the frame left unprocessed for the before/after wipe; 0 — off |
| `theme` | `light` / `dark` |
| `open_menu_on_start` | open the menu on launch; `false` — a short alert instead |
| `hotkeys` | `{"toggle": "Num1", ...}` — see README, "Using it". Names: `Num0`-`Num9`, `Numdot`, `Numplus`, `Numminus`, `Nummul`, `Numdiv`, `F1`-`F12`, `Insert`, `Home`, letters, digits, with `Ctrl+`/`Alt+`/`Shift+` |
| `menu_offset`, `menu_scale`, `menu_height` | where the menu sits, its scale and height. Written by the app, not meant to be edited by hand (`menu_height: null` — fit the content) |

## Architecture

Two processes. Python drives settings, optical-flow guides and the menu
layer; the C++ worker owns the D3D12 device, the capture, NGX and the
overlay window.
They talk over stdin/stdout with a binary protocol:

| Message | Purpose |
|---|---|
| `D5V3` | stream header: sizes, profile, NR parameters |
| `SHMI` / `SACK` | shared-memory section name for the input frame |
| `WNDO` / `WACK` | raise/close the worker's output window |
| `MOTS` / `MACK` | motion arrives at reduced size, worker upscales it on GPU |
| `DDA1` / `DACK` | worker takes over capture (Desktop Duplication), colour never touches the CPU |
| `GRAY` / `GAK` | worker writes AREA-downsampled luminance (320×180) into a back-mapping for the guides |
| `FRM1` | frame: header, then either payload (RGBA8 + motion) or "in shared memory" flag |
| `OUT1` | result: RGBA8 full-res, or `bytes=0` — the worker already presented it |
| `RNSZ` / `RACK` | change work resolution on the fly, no process restart |
| `OUTS` / `OAK2` | named section the worker writes result pixels into; the reply then carries `bytes = 0xFFFFFFFF` instead of a payload |

**Capture.** On `DDA1` the worker opens Desktop Duplication on the GPU: each
frame is copied into a cross-device shared texture and swizzled to RGBA.
Python stops capturing entirely — `grab` and `guides` drop to 0.1 ms.
Fallback (dxcam + full-frame send) stays intact.

**Guides.** The optical flow needs a small gray frame. On `GRAY` the worker
computes it with an honest block average (12×12 per cell at 4K → 320×180,
matching `cv2.INTER_AREA`; bilinear would alias text and break the flow) and
writes it into a named mapping. No 4K frame ever crosses the CPU.

**Output.** On `WNDO` the worker raises its own borderless D3D12-swapchain
window across the screen and presents the NGX result itself: pixels never
return to Python. The pygame window stays as the menu layer — its background
is filled with a chroma key and made transparent (`LWA_COLORKEY`). While the
menu is open the window's global alpha (`LWA_ALPHA`) goes to 255, otherwise
the bright frame underneath bleeds through the panel.

**NR off (bypass).** `Num1` does not stop the pipeline anymore. Frames are
sent with `FRAME_FLAG_BYPASS`: the worker skips the NGX evaluate and
presents the raw capture instead. The overlay stays alive; everything is
hidden only on real exit.

**Recording path.** Frames are requested from the worker with
`FRAME_FLAG_WANT_PIXELS` (the same mechanism as screenshots), the open menu
is drawn onto the frame with `draw_capture_overlay()`, then PyAV encodes
AV1 NVENC.

Two constraints that look like quirks but are mandatory:

- **The worker binary must be named `nvngx.dll`.** NGX Core returns
  `FAIL_PlatformError` on `Init_Ext` for any other process name. Verified
  experimentally.
- **Work resolution is capped at 2560×1440.** At 4K the feature 18 goes
  silent: the worker hangs on frame zero in both legacy and upscale modes.

Scale changes go through `RNSZ` (~60 ms, the worker recreates the NGX
feature in-process). If `RNSZ` fails — fall back to a full worker restart.

## Performance

Measured on RTX 5070 Ti, 4K desktop, `work_scale` 0.5 (1920×1080), pipeline
fully on the GPU (DDA + GRAY + WNDO + MOTS). Worker-side phase breakdown
(enable with `NS_PHASE=1`):

```
                 acq   dda  upload   eval  present   frame     FPS
NR ON            0.0   0.7     0.1   16.6      0.5    17.9      55
NR OFF (bypass)  ~3    ~4      0.1      -      ~3      7.3  121-133
```

**NGX evaluation is 16.6 ms — 93% of an NR frame.** Everything else together
costs 1.3 ms, so the practical ceiling on this hardware is set by NGX, not by
the plumbing. In bypass mode NGX is skipped and the loop waits on the desktop
actually changing (`acq`), which is why it runs several times faster.

An earlier revision of this section claimed "NGX itself is ~1 ms". That was a
measurement error: the figure came from regressing round-trip time against
work resolution, and such a regression only sees the resolution-dependent part
(0.46 ms/MPix). NGX's large constant cost was invisible to it and got
attributed to transport.

The 1440p figures previously published here (64 FPS) predate the DDA fence
fix and understate current performance; they have not been re-measured.

### work_scale costs nothing (in upscale mode)

In the legacy upscale mode (nr_small off) NGX evaluation time does not depend
on the work resolution at all. Measured across the whole slider range on a 4K
desktop:

```
work          MPix   eval ms    FPS
960x540       0.52    16.13    53.8
1344x756      1.02    15.90    55.4
1728x972      1.68    15.93    56.8
2112x1188     2.51    15.96    55.7
2496x1404     3.50    15.98    55.3
```

Fit: `eval = 15.97 ms + (-0.01) ms/MPix`, R² = 0.011 — i.e. noise, not a
trend. Seven times more input pixels cost 0.2 ms, which is within the
measurement spread.

**So in upscale mode the slider is a quality control, not a speed control.**
Lowering `work_scale` buys no performance and only costs sharpness. The work
size is clamped to the 2560×1440 NGX cap anyway, so the slider always lands
exactly at the cap: 2560×1440 from a 4K desktop, 2304×1440 from 2560×1600.

Re-confirmed with D3D12 timestamps on the queue, i.e. GPU time inside
`Evaluate` rather than time around the submit, on two desktop resolutions:

```
desktop      work_scale range   work MPix      eval GPU
2560x1600    0.30 .. 1.00       0.37 .. 3.32   7.9 - 8.6 ms
3840x2160    0.30 .. 1.00       0.75 .. 3.69   15.70 - 15.73 ms
```

Five to nine times the work pixels for the same time, at either resolution.

### ...but the screen resolution does

The two rows above differ by 2.02× in screen pixels and by 1.96× in eval
time. That is the whole story: in upscale mode NGX is handed the **full-res**
frame and returns a full-res frame, downsampling to the work size internally.
So the cost is set by the desktop, not by the slider.

An earlier revision of this section concluded "the model works at its own
fixed internal resolution". That was wrong, and wrong in an instructive way:
it came from varying only `work_scale` at a single desktop resolution, which
by construction cannot see a dependence on the frame size.

Consequences: in this mode a 4K desktop pays a floor of 15.7 ms of NGX per
frame, about 47 FPS end to end, and no amount of plumbing gets near a 144 Hz
panel. On 2560×1600 the same floor is 8.0 ms.

### Processing at a reduced resolution

That floor is not a law, though. The network is **same-resolution — it
enhances, it does not upscale**, so its cost tracks the pixel count it is
handed, and "upscaling" mode hands it the whole screen. Measured in isolation
by feeding the worker different frame sizes directly:

```
work          MPix   eval GPU
1280x720      0.92    2.90 ms
1920x1080     2.07    4.60 ms
2560x1440     3.69    7.10 ms
```

Fit: `eval = 1.50 ms + 1.51 ms/MPix`, which also predicts the two numbers
above (14.0 ms at 4K, 7.7 ms at 2560×1600) and matches what the
[neural-upstream](https://github.com/matiasLombo/neural-upstream) add-on
measures for the same network in games.

So **Process at reduced resolution** (menu → speed, `"nr_small"` in
`config.json`) scales the frame down to the work resolution, runs the network
there, and scales the result back up. On a 4K desktop, work at the 2560×1440
cap:

```
                 eval GPU    FPS
full screen       16.05     42.9
reduced            7.25     65.3
```

**Off by default** (set it with the resolution slider in the menu), and the
softness that used to come with a reduced work size is gone: the result is
composed onto the pristine 1:1 native frame by the **matched residual
composite** (see below), so text and edges keep full resolution while the
cheap low-res network does the relighting.
Measured end to end on the 4K desktop: 47.9 FPS at full screen vs 71.9 FPS at
work_scale 0.65 with the composite — a 50% gain with the native anchor intact.

With it on, **Work scale** finally does something — it is the resolution the
network actually sees. With it off the slider is inert, which is exactly what
the measurements at the top of this section were showing all along.

### Matched residual composite

The composite is the DLSSNR-Cost-Scaler principle applied to the desktop:
instead of stretching the low-res network result up, the worker writes

```
result = native + (nr_out - nr_in) * strength
```

at full resolution — the neural delta (what the network changed) lands on the
pristine 1:1 native frame. The native frame stays the anchor, so text, edges
and UI keep full sharpness while the network runs at the cheap work
resolution. Measured on a synthetic detail pattern (test_residual.py):
Laplacian detail 983 with the composite vs 88 with the plain bilinear
upscale (input 951), and a hard edge 87.8 vs 59.1. `strength` is fixed at
1.0; `NS_NR_RESIDUAL=0` forces the plain upscale path for the tests.


## Before / after wipe

The **Before / after wipe** slider leaves the left share of the frame
unprocessed, so the raw capture and the NGX result sit side by side with an
accent-coloured divider between them. 0 turns it off.

It happens in the worker, on the GPU: one `CopyTextureRegion` of the left
strip of the input over the output, then the divider through
`ClearUnorderedAccessViewFloat` with a rect. Both run before Present and
before pixels are handed back, so the wipe lands in recordings and
screenshots by itself. The position rides in the top 16 bits of the frame
header's flag field, so moving the slider does not recreate the worker.

## Older GPUs (20/30/40-series)

`nvngx_dlssnr.dll` refuses to create the feature on anything below Blackwell.
Its own version resource says `NGXGpuArchitecture = NVSDK_NGX_GPU_Arch_Blackwell2`,
and it carries the message

```
DLSSNR: Unsupported GPU architecture 0x%x, minimum required 0x%x
```

The bundled build is the leaked **310.8.0** runtime (from the
RankFTW/rhi-repo mirror): parsing its fatbin headers shows `sm_75/86/89/120`
kernels - the universal build. The architecture hook (below) makes the
feature create succeed on non-Blackwell cards; the refusal on cards the
kernels cannot run on (Turing) is a policy check, not missing code.

The library learns the architecture through nvapi — it loads `nvapi64.dll`,
takes its single export `nvapi_QueryInterface` and asks for
`NvAPI_GPU_GetArchInfo` by id. The worker patches that one function in **its
own process memory** at startup: the prologue is saved, replaced with a jump
to our handler, and restored around every real call, so any GPU handle is
still served by NVIDIA's own code — only the returned architecture is
rewritten to Blackwell. Nothing in NVIDIA's files is modified, and on a
50-series card the hook disables itself and does nothing.

**On by default** (set `NS_ARCH_SPOOF=0` to disable). **Confirmed working on
a 40-series card** by a user who ran it; 20- and 30-series are still
unverified. Nothing here can test any of them — the only card on this machine
is a 5070 Ti, where the hook disables itself by design. The menu's GPU dot
tells you the truth either way: it goes green only when the worker actually
created feature 18, not when the architecture merely looks right.

This likely conflicts with the license terms of NVIDIA's redistributable. It
defeats no copy protection and modifies no files, but enabling it is your
call.

## Building the worker

```
native\build-host.bat
```

Requires MSVC 2022 Build Tools at
`C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`. The script
builds `dlss5-feed-host64.cpp` into `native/nvngx.dll`, linking
`native/lib/Windows_x86_64/x64/nvsdk_ngx_d.lib`. NGX headers are in
`native/include/`.

The artifact `native/nvngx.dll` is not committed to the repo.
