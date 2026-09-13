"""A focused topmost target stays below both NeuralScreen output layers.

This is an end-to-end z-order check against the public Windows surface.  The
helper owns a repainting borderless topmost red window.  The real application
is launched through the bundled pythonw.exe, then the exact worker and HUD
windows are checked in EnumWindows order while the target remains focused.

The two helper modes are deliberately private to this file so the test does
not need another tracked program: one is the topmost repainting game target,
the other is a small visible focus-away window.  Both have exact PIDs, which
keeps cleanup away from unrelated pythonw.exe processes.

Run:  runtime\\python.exe tests\\test_focus_z_order.py
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PY = BASE / "runtime" / "python.exe"
PYW = BASE / "runtime" / "pythonw.exe"
CONFIG = BASE / "config.json"
LOG = BASE / "NeuralScreen.log"

TARGET_TITLE = "NeuralScreen focus z-order target"
TARGET_SIZE = (960, 540)
TARGET_POS = (120, 120)
TARGET_RED = (220, 48, 48)
AWAY_TITLE = "NeuralScreen focus z-order away"
AWAY_SIZE = (420, 240)
AWAY_POS = (1200, 160)

HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOACTIVATE = 0x0010
VK_NUMLOCK = 0x90
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_Q = 0x51
KEYEVENTF_KEYUP = 0x0002
MUTEX_QUERY_STATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WORKER_PROCESS_ACCESS = (PROCESS_QUERY_LIMITED_INFORMATION |
                          PROCESS_TERMINATE | SYNCHRONIZE)
STILL_ACTIVE = 259
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
WAIT_FAILED = 0xFFFFFFFF

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
user32.mouse_event.restype = None
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                               ctypes.c_size_t]
user32.keybd_event.restype = None
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = wintypes.SHORT
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


def _text(hwnd: int) -> tuple[str, str]:
    cls = ctypes.create_unicode_buffer(128)
    title = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, len(cls))
    user32.GetWindowTextW(hwnd, title, len(title))
    return cls.value, title.value


def _pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _windows() -> list[tuple[int, int, str, str]]:
    rows: list[tuple[int, int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        hwnd_i = int(hwnd)
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return True
        cls, title = _text(hwnd_i)
        rows.append((hwnd_i, _pid(hwnd_i), cls, title))
        return True

    user32.EnumWindows(callback, 0)
    return rows


def _find_window(class_name: str, title: str | None = None,
                 *, pid: int = 0, exclude_pid: int = 0) -> int:
    for hwnd, hwnd_pid, cls, text in _windows():
        if cls != class_name or (title is not None and text != title):
            continue
        if pid and hwnd_pid != pid:
            continue
        if exclude_pid and hwnd_pid == exclude_pid:
            continue
        return hwnd
    return 0


def _find_present() -> tuple[int, int]:
    hwnd = _find_window("NeuralScreenPresent", "NeuralScreen")
    return hwnd, _pid(hwnd) if hwnd else 0


def _find_hud(exclude_pid: int) -> tuple[int, int]:
    hwnd = _find_window("pygame", "NeuralScreen", exclude_pid=exclude_pid)
    return hwnd, _pid(hwnd) if hwnd else 0


def _z_order() -> list[int]:
    return [hwnd for hwnd, _pid_value, _cls, _title in _windows()]


def _order_text(hwnds: dict[str, int]) -> str:
    rows = _windows()
    by_hwnd = {hwnd: (idx, pid, cls, title)
               for idx, (hwnd, pid, cls, title) in enumerate(rows)}
    parts = []
    for name, hwnd in hwnds.items():
        row = by_hwnd.get(hwnd)
        parts.append(f"{name}={row if row else ('missing', hwnd)}")
    return "; ".join(parts)


def _wait_window(fn, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {description}")


def _focus(hwnd: int, *, click: bool = True) -> bool:
    """Focus a real window, using input when SetForegroundWindow is blocked."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    for _attempt in range(6):
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.08)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        if not click:
            continue
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            continue
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        user32.SetCursorPos(cx, cy)
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.12)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
    return int(user32.GetForegroundWindow() or 0) == hwnd


def _tap(vk: int, modifiers: tuple[int, ...] = ()) -> None:
    for modifier in modifiers:
        user32.keybd_event(modifier, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    for modifier in reversed(modifiers):
        user32.keybd_event(modifier, 0, KEYEVENTF_KEYUP, 0)


def _numlock_on() -> bool:
    return bool(user32.GetKeyState(VK_NUMLOCK) & 1)


def _toggle_numlock() -> None:
    user32.keybd_event(VK_NUMLOCK, 0, 0, 0)
    user32.keybd_event(VK_NUMLOCK, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def _restore_cursor(cursor: wintypes.POINT) -> bool:
    for _attempt in range(5):
        user32.SetCursorPos(cursor.x, cursor.y)
        restored = wintypes.POINT()
        if (user32.GetCursorPos(ctypes.byref(restored)) and
                (restored.x, restored.y) == (cursor.x, cursor.y)):
            return True
        time.sleep(0.05)
    return False


def _log_offset() -> int:
    try:
        return LOG.stat().st_size
    except FileNotFoundError:
        return 0


def _log_since(offset: int) -> str:
    try:
        with LOG.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()
    except FileNotFoundError:
        return ""


def _wait_fresh_nr(offset: int, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        text = _log_since(offset)
        if "NR OFF" in text:
            raise RuntimeError("fresh launch log contains NR OFF")
        if "NR ON | FPS" in text:
            return text
        time.sleep(0.25)
    raise RuntimeError("no fresh NR ON | FPS output")


def _wait_newer_nr(offset: int, previous_count: int,
                   timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _log_since(offset)
        if "NR OFF" in text:
            raise RuntimeError("fresh log contains NR OFF")
        if text.count("NR ON | FPS") > previous_count:
            return text
        time.sleep(0.25)
    raise RuntimeError("NR ON | FPS output did not advance after the focus cycles")


def _mutex_exists() -> bool:
    handle = kernel32.OpenMutexW(MUTEX_QUERY_STATE, False,
                                 "NeuralScreen_SingleInstance")
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def _open_worker_handle(pid: int):
    if not pid or pid == os.getpid():
        raise RuntimeError(f"invalid worker PID {pid}")
    handle = kernel32.OpenProcess(WORKER_PROCESS_ACCESS, False, pid)
    if not handle:
        raise RuntimeError(f"could not retain worker PID {pid}")
    return handle


def _worker_alive(handle) -> bool:
    if not handle:
        return False
    code = wintypes.DWORD()
    try:
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RuntimeError("GetExitCodeProcess failed")
        return code.value == STILL_ACTIVE
    except Exception as exc:
        raise RuntimeError(f"worker liveness check failed: {exc}") from exc


def _cleanup_child(proc, label: str, failures: list[str]) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        failures.append(f"{label} pid {proc.pid} cleanup timed out")
    except Exception as exc:
        failures.append(f"{label} cleanup failed: {exc}")
    try:
        if proc.poll() is None:
            failures.append(f"{label} pid {proc.pid} is still alive")
    except Exception as exc:
        failures.append(f"{label} liveness check failed: {exc}")


def _cleanup_worker(handle, label: str, failures: list[str]) -> None:
    if not handle:
        return
    try:
        if _worker_alive(handle):
            wait_result = kernel32.WaitForSingleObject(handle, 8000)
            if wait_result == WAIT_TIMEOUT:
                if not kernel32.TerminateProcess(handle, 1):
                    failures.append(f"{label} TerminateProcess failed")
                else:
                    wait_result = kernel32.WaitForSingleObject(handle, 8000)
            if wait_result == WAIT_FAILED:
                failures.append(f"{label} WaitForSingleObject failed")
            elif wait_result == WAIT_TIMEOUT:
                failures.append(f"{label} is still alive after termination")
            elif wait_result != WAIT_OBJECT_0:
                failures.append(f"{label} wait returned {wait_result}")
    except Exception as exc:
        failures.append(f"{label} cleanup failed: {exc}")
    try:
        if _worker_alive(handle):
            failures.append(f"{label} is still alive")
    except Exception as exc:
        failures.append(f"{label} liveness check failed: {exc}")


def _target_mode() -> int:
    """Private helper: repaint a constant red topmost target."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    import pygame

    pygame.init()
    screen = pygame.display.set_mode(TARGET_SIZE, pygame.NOFRAME)
    pygame.display.set_caption(TARGET_TITLE)
    hwnd = int(pygame.display.get_wm_info()["window"])
    x, y = TARGET_POS
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, TARGET_SIZE[0], TARGET_SIZE[1],
                         SWP_NOACTIVATE)
    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return 0
            screen.fill(TARGET_RED)
            pygame.display.flip()
            time.sleep(0.03)
    finally:
        pygame.quit()


def _away_mode() -> int:
    """Private helper: a small visible window used only for focus-away."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass
    import pygame

    pygame.init()
    screen = pygame.display.set_mode(AWAY_SIZE, pygame.NOFRAME)
    pygame.display.set_caption(AWAY_TITLE)
    hwnd = int(pygame.display.get_wm_info()["window"])
    x, y = AWAY_POS
    user32.SetWindowPos(hwnd, 0, x, y, AWAY_SIZE[0], AWAY_SIZE[1], SWP_NOACTIVATE)
    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return 0
            screen.fill((48, 72, 112))
            pygame.display.flip()
            time.sleep(0.03)
    finally:
        pygame.quit()


def _wait_z_order(hud: int, picture: int, target: int, timeout: float = 1.0) -> None:
    """Require top-to-bottom HUD, worker picture, target within one second."""
    deadline = time.monotonic() + timeout
    wanted = (hud, picture, target)
    while time.monotonic() < deadline:
        order = _z_order()
        positions = [order.index(hwnd) if hwnd in order else -1 for hwnd in wanted]
        if all(pos >= 0 for pos in positions) and positions[0] < positions[1] < positions[2]:
            return
        time.sleep(0.02)
    raise RuntimeError(
        "target is not below the worker picture and HUD within 1s: "
        f"{_order_text({'HUD': hud, 'picture': picture, 'target': target})}")


def main() -> int:
    if "--target" in sys.argv:
        return _target_mode()
    if "--away" in sys.argv:
        return _away_mode()

    if not PY.exists() or not PYW.exists():
        print("FAIL: tracked runtime is missing")
        return 1
    if _mutex_exists() or _find_present()[0] or _find_hud(os.getpid())[0]:
        print("FAIL: NeuralScreen is already running (mutex or exact output window)")
        return 1

    original_config: bytes | None = None
    original_foreground: int | None = None
    cursor: wintypes.POINT | None = None
    original_numlock: bool | None = None
    target = None
    away = None
    app = None
    worker_pid = 0
    worker_handle = None
    result = 1
    cleanup_failures: list[str] = []
    try:
        original_foreground = int(user32.GetForegroundWindow() or 0)
        cursor = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(cursor)):
            raise RuntimeError("could not save the cursor position")
        original_numlock = _numlock_on()
        original_config = CONFIG.read_bytes()
        if not original_numlock:
            _toggle_numlock()

        cfg = json.loads(original_config.decode("utf-8"))
        cfg.update({
            "worker_present": True,
            "capture_in_worker": True,
            "fullscreen": True,
            "open_menu_on_start": True,
        })
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        log_offset = _log_offset()

        target = subprocess.Popen(
            [str(PY), str(Path(__file__).resolve()), "--target"],
            cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        target_hwnd = _wait_window(
            lambda: _find_window("pygame", TARGET_TITLE, pid=target.pid),
            10.0, "the exact repainting target window")
        if not _focus(target_hwnd):
            raise RuntimeError("could not focus the repainting target")

        away = subprocess.Popen(
            [str(PY), str(Path(__file__).resolve()), "--away"],
            cwd=str(BASE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        away_hwnd = _wait_window(
            lambda: _find_window("pygame", AWAY_TITLE, pid=away.pid),
            10.0, "the exact focus-away helper window")

        app = subprocess.Popen([str(PYW), str(BASE / "main.py")], cwd=str(BASE),
                               creationflags=subprocess.CREATE_NO_WINDOW)
        text = _wait_fresh_nr(log_offset)
        nr_on_count = text.count("NR ON | FPS")

        picture, worker_pid = _wait_window(_find_present, 30.0,
                                           "the exact NeuralScreenPresent window")
        worker_handle = _open_worker_handle(worker_pid)
        hud, hud_pid = _wait_window(lambda: _find_hud(os.getpid()), 15.0,
                                     "the HUD excluding the test PID")
        if hud_pid == os.getpid():
            raise RuntimeError("HUD belongs to the test PID")
        if worker_pid == os.getpid() or worker_pid == target.pid:
            raise RuntimeError(f"present window has a helper PID ({worker_pid})")

        # Keep the forced startup menu open while the target remains the real
        # foreground window. The HUD must stay activatable without taking focus.
        if not _focus(target_hwnd):
            raise RuntimeError("could not focus the target with the startup menu open")
        _wait_z_order(hud, picture, target_hwnd)
        if int(user32.GetForegroundWindow() or 0) != target_hwnd:
            raise RuntimeError("target lost foreground after the initial z-order check")

        if not _focus(away_hwnd):
            raise RuntimeError("the exact focus-away helper did not take foreground")
        if int(user32.GetForegroundWindow() or 0) != away_hwnd:
            raise RuntimeError("the exact focus-away helper did not retain foreground")
        for cycle in range(1, 6):
            if not _focus(away_hwnd):
                raise RuntimeError(f"focus-away failed on cycle {cycle}")
            if not _focus(target_hwnd):
                raise RuntimeError(f"focus-back failed on cycle {cycle}")
            if int(user32.GetForegroundWindow() or 0) != target_hwnd:
                raise RuntimeError(f"target did not retain foreground on cycle {cycle}")
            current_picture, current_pid = _find_present()
            if not current_picture or current_pid != worker_pid:
                raise RuntimeError(
                    f"worker PID changed on cycle {cycle}: {worker_pid} -> {current_pid}")
            current_hud, _ = _find_hud(os.getpid())
            if not current_hud:
                raise RuntimeError(f"HUD disappeared on cycle {cycle}")
            _wait_z_order(current_hud, current_picture, target_hwnd)
            foreground = int(user32.GetForegroundWindow() or 0)
            if foreground != target_hwnd:
                raise RuntimeError(
                    f"target lost foreground on cycle {cycle}; "
                    f"foreground={(foreground, _pid(foreground), *_text(foreground))}; "
                    f"{_order_text({'HUD': current_hud, 'picture': current_picture, 'target': target_hwnd})}")
            print(f"cycle {cycle}/5: HUD > picture > target, worker pid {current_pid}")

        tail = _wait_newer_nr(log_offset, nr_on_count)
        print(f"PASS: HUD > picture > target for five focus cycles, worker pid {worker_pid}; "
              f"fresh NR ON lines {nr_on_count}->{tail.count('NR ON | FPS')}")
        result = 0
    except Exception as exc:
        print(f"FAIL: {exc}")
    finally:
        # Every cleanup action is independent and uses the retained process
        # object or worker handle, so PID reuse cannot target another process.
        try:
            if app is not None and app.poll() is None:
                _tap(VK_Q, (VK_CONTROL, VK_MENU))
        except Exception as exc:
            cleanup_failures.append(f"quit hotkey failed: {exc}")
        try:
            if app is not None and app.poll() is None:
                app.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            cleanup_failures.append(f"NeuralScreen main wait failed: {exc}")
        _cleanup_child(app, "NeuralScreen main", cleanup_failures)
        _cleanup_worker(worker_handle, "NeuralScreen worker", cleanup_failures)
        try:
            if worker_handle is not None:
                if not kernel32.CloseHandle(worker_handle):
                    cleanup_failures.append("NeuralScreen worker handle close failed")
                worker_handle = None
        except Exception as exc:
            cleanup_failures.append(f"NeuralScreen worker handle close failed: {exc}")
        try:
            if original_config is None:
                cleanup_failures.append("config baseline was not captured")
            else:
                CONFIG.write_bytes(original_config)
                if CONFIG.read_bytes() != original_config:
                    cleanup_failures.append("config.json bytes were not restored")
                else:
                    print("config restored: yes")
        except Exception as exc:
            cleanup_failures.append(f"config restore failed: {exc}")

        try:
            if cursor is None:
                cleanup_failures.append("cursor baseline was not captured")
            elif not _restore_cursor(cursor):
                actual = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(actual))
                cleanup_failures.append(
                    f"cursor was not restored after retries: "
                    f"wanted={(cursor.x, cursor.y)}, actual={(actual.x, actual.y)}")
        except Exception as exc:
            cleanup_failures.append(f"cursor restore failed: {exc}")

        try:
            if original_numlock is None:
                cleanup_failures.append("Num Lock baseline was not captured")
            else:
                if _numlock_on() != original_numlock:
                    _toggle_numlock()
                if _numlock_on() != original_numlock:
                    cleanup_failures.append("Num Lock state was not restored")
        except Exception as exc:
            cleanup_failures.append(f"Num Lock restore failed: {exc}")

        _cleanup_child(target, "focus target helper", cleanup_failures)
        _cleanup_child(away, "focus-away helper", cleanup_failures)

        try:
            if original_foreground and user32.IsWindow(original_foreground):
                if not _focus(original_foreground, click=False):
                    cleanup_failures.append("foreground restore call failed")
                elif int(user32.GetForegroundWindow() or 0) != original_foreground:
                    cleanup_failures.append("foreground was not restored")
        except Exception as exc:
            cleanup_failures.append(f"foreground restore failed: {exc}")

        if cleanup_failures:
            for failure in cleanup_failures:
                print(f"FAIL: cleanup: {failure}")
            result = 1

    return result


if __name__ == "__main__":
    raise SystemExit(main())
