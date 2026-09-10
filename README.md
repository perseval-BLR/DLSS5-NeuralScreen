# NeuralScreen

**NVIDIA's DLSS 5 neural renderer, applied to your whole Windows desktop in
real time.** Everything on screen — games, video, photos — goes through the
same neural network that DLSS 5 games use, and comes back sharper.

> **Read this first:** the short guide below gets you running. The full
> technical story — how it works, what was measured, why it is built this
> way — lives in **[TECHNICAL.md](TECHNICAL.md)**. English:
> **[README.md](README.md)**. Русская версия: **[README.ru.md](README.ru.md)**
> / **[TECHNICAL.ru.md](TECHNICAL.ru.md)**.

<table>
<tr>
<td><img src="docs/screenshot-main-light.png" alt="Menu, light theme" width="420"></td>
<td><img src="docs/screenshot-main-dark.png" alt="Menu, dark theme" width="420"></td>
</tr>
</table>

*Everything lives in one menu inside the overlay. The **Before / after wipe**
slider splits the screen down the middle so you can see what the effect is
actually doing.*

## What you need

- **Windows 11**
- **An NVIDIA RTX card.** What works, honestly:

  | Cards | Status |
  |---|---|
  | **RTX 50** (Blackwell) | ✅ works - the officially supported generation |
  | **RTX 40** (Ada) | ✅ works - through the built-in architecture hook |
  | **RTX 30** (Ampere) | ✅ works - restored in v1.5.1 (the universal runtime + spoof 0x1B0, same stack as v1.3.0) |
  | **RTX 20** (Turing) | ❌ cannot run the neural pass at all - below the minimum architecture (DLSS5-Feeder issue #73). See *Trying RTX 20* below. |
  | **Laptops with hybrid graphics (Optimus)** | ⚠️ works only when the display is driven by the NVIDIA GPU - force the dGPU (MUX switch, or an external monitor on the dGPU port). On the iGPU it fails on the first frame |

- **Nothing installed.** The release archive brings its own Python.

### Trying RTX 20

Turing is below the minimum architecture - no runtime build makes the
neural pass run on it (0xBAD00001, FeatureNotSupported). The program
will start and the menu works; the picture is not processed. No workaround
exists.

## Install

1. Download the archive from [Releases](https://github.com/perseval-BLR/DLSS5-NeuralScreen/releases)
   and unpack it anywhere. **Everything is inside** — including NVIDIA's
   `nvngx_dlssnr.dll` (158 MB, too big for GitHub to keep in the repository,
   so it ships in the archive instead).
2. Run **`NeuralScreen.exe`**.

Windows will probably warn you about an unknown publisher the first time — the
program is not signed with a paid certificate. Click *More info* → *Run
anyway*. If you would rather not, `NeuralScreen.vbs` next to it does the same
thing.

There is no installer and nothing is written outside the folder. To remove it,
delete the folder.

> **Do not use it in competitive online games.** A process named `nvngx.dll`
> plus a fullscreen overlay is exactly what anti-cheat systems look for.

## Using it

The program sits in the tray (with a taskbar button) and draws over your
desktop. Press **Num2** for the menu. The hotkeys live on the numpad, so
**Num Lock has to be on** - with it off those keys send Insert/End/arrows
instead and nothing happens.

| Key | What it does |
|---|---|
| **Num2** | open / close the menu |
| **Num1** | neural rendering on / off |
| **Num3** | screenshot |
| **Num0** | start / stop recording, with sound |
| **Num4** / **Num6** | processing resolution down / up |
| **Num5** | capture the window under the cursor (see below) |
| **Ctrl+Alt+Q** | quit |

Every key can be reassigned in the menu, under the sliders icon.

While the menu is open it takes the mouse and keyboard, so it works on top of a
game. Closed, clicks go straight through it as if it were not there.

Startup and mode switches do not flash: the overlay appears with the first
real frame, a brief blur-with-spinner covers the pipeline rebuild.

### Whole screen or one window

NeuralScreen renders the whole screen by default. To process one window
instead (a game, a browser), pick **Select window...** in the menu - the
list shows every open window, hovering highlights it on the screen, and
the overlay follows the window. Back to the whole screen: **Fullscreen**
in the menu.

**Num5** is a shortcut for the common case: the window you want is already
open and covers the screen - point at it and press **Num5** to capture it
directly. For anything else, use the window list in the menu.

Minimising the window pauses processing.

## The menu

<table>
<tr>
<td><img src="docs/screenshot-windows.png" alt="Window list" width="380"></td>
<td><img src="docs/screenshot-settings.png" alt="Settings page" width="380"></td>
</tr>
</table>

The dot next to your graphics card is green when neural rendering is actually
running on it, red when it is not.

The settings worth touching:

- **Profile** — how strong the effect is, from *Faithful* to *Extreme*. The
  default is *Natural* - a faithful, balanced look. The four sliders
  underneath are the same thing in detail.
- **Before / after wipe** — leaves the left part of the screen untouched so
  you can see what the effect is doing. Set it back to 0 when done.
- **Resolution the network runs at** — one slider. At the top it is your whole
  screen, which is the default and the best picture. Every step down hands the
  network a smaller frame: with the slider off the default (full screen) the
  network is already at its best, and every step down means **roughly 50% more
  frames** at 2560×1440 on a 4K screen. The picture stays sharp: the network's
  result is composed onto the pristine 1:1 native frame (a matched residual
  composite), so text, edges and UI keep full resolution while the cheap
  low-res network does the relighting. Look at your own screen and pick a step.

The interface speaks **12 languages** - English, Russian, French, German,
Spanish, Italian, Portuguese, Polish, Ukrainian, Chinese, Japanese and Korean.
Pick one under the sliders icon (Language). The menu, the HUD and the alerts
all follow.

Everything else — which monitor, whether the menu opens on launch, starting
with Windows, key assignments — is behind the sliders icon.

## Recording and screenshots

**Num0** records what you see, with system sound, into an MP4 in
`recordings`. **Num3** saves a screenshot. The menu shows up in both if
open - on purpose. The recording audio passes through a soft limiter, so a
loud system mix cannot clip the track into distortion.

**Record the processed picture externally:**
- **OBS (recommended):** launch with `NS_SPOUT=1` (environment variable,
  e.g. `set NS_SPOUT=1` then `NeuralScreen.exe`). The worker publishes the
  output as a Spout2 shared texture - add a **Spout2 Capture** source in OBS
  (free plugin) and record. Works in full-screen mode too. Off by default.
- **NVIDIA App / OBS display capture:** use one-window mode - pick the
  window in the menu (or point at it and press **Num5**), record, then
  switch back to **Fullscreen** when done. In this mode the overlay is
  visible to screen capture; in full-screen mode it hides (the program
  captures the screen itself, and a visible overlay would feed on itself).

## If something is not working

**Nothing appears after launch.** Check `NeuralScreen.log` next to the
program; the most common cause is a missing `native\nvngx_dlssnr.dll`.

**The overlay is invisible in a game.** True fullscreen cannot have anything
drawn over it — a Windows rule. Switch the game to *borderless* or
*windowed fullscreen*.

**The menu pointer is missing or frozen.** A fullscreen game hides the system
cursor; the overlay only shows the system cursor. Borderless fixes it.

**The numpad hotkeys do nothing.** They need *Num Lock* to be on. With Num
Lock off the numpad sends Insert/End/arrows and the keys simply do not exist.

**The picture is soft.** Put *Resolution the network runs at* back to the top
of its slider.

**A key does nothing.** Something else claimed it; reassign it in the menu
under the sliders icon.

**The menu is slow in a heavy game.** At 4K the pipeline can take up to a
second per frame; a press may feel lost. The picture is the priority.

## Known limitations

- **True fullscreen games** cannot have the overlay drawn over them — a Windows rule. Borderless or windowed only.
- **A second instance is not guarded** — close the first one first.
- **The window list** shows every visible window; Num5 takes the one under the cursor. **A second monitor** works but was not tested with a window between them.
- **HDR displays** are not supported: switch to SDR (Win+Alt+B).
- **Pipeline latency** is 40–60 ms — fine interactively, not competitively; **processing resolution is capped at 2560×1440** (the network refuses 4K), output is always your full native resolution.
- **The bundled `nvngx_dlssnr.dll` is the leaked 310.8.0 runtime carrying sm_75/86/89/120 kernels (RTX 20-50)** — see License below.

## License

The code here is MIT. NVIDIA's `nvngx_dlssnr.dll` is the leaked 310.8.0
runtime (sm_75/86/89/120 kernels, RTX 20-50). Included as-is, no
guarantees; research-only.