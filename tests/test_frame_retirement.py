r"""Exercise real frame retirement and a Close-failed list without invalid GPU execution.

Run: runtime\python.exe tests\test_frame_retirement.py
"""
import ctypes
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

import protocol as p
from main import PROFILES
from paths import WORKER_EXE


def check_hdr_transition_guard():
    source = (ROOT / 'native/dlss5-feed-host64.cpp').read_text(encoding='utf-8')
    # Without the declaration: `got` stopped being const when the
    # WANT_PIXELS dry-spell retry was added, and what this check is about is
    # the ORDER - the frame's format has to be known before the deferral is
    # chosen - not how the variable is spelled.
    acquire = source.index('got = g_wgc_active ? WgcGrab(v) : DdaGrab(v);')
    decide = source.index('defer_tail = !g_hdr_capture', acquire)
    upload = source.index('UploadMotionOnly(v, mv_ptr', decide)
    assert acquire < decide < upload, 'tail deferral must use the acquired frame format'
    print('OK: HDR capture format is known before tail deferral is selected')


def check_close_failure():
    """Include the actual shared functions so the check cannot mirror a fake implementation."""
    vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    install = subprocess.check_output([str(vswhere), '-latest', '-products', '*',
                                      '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                                      '-property', 'installationPath'], text=True).strip()
    vcvars = Path(install) / 'VC/Auxiliary/Build/vcvars64.bat'
    assert vcvars.is_file(), 'MSVC compiler environment unavailable'
    with tempfile.TemporaryDirectory(prefix='ns-retirement-') as temp:
        work = Path(temp)
        source = work / 'close_check.cpp'
        source.write_text(r'''
#define main neural_app_main
#include "SOURCE"
#undef main
#include <cassert>
int main() {
    SetEnvironmentVariableA("NS_PHASE", "1");
    assert(SUCCEEDED(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_11_0, IID_PPV_ARGS(&h.dev))));
    D3D12_COMMAND_QUEUE_DESC q = {};
    assert(SUCCEEDED(h.dev->CreateCommandQueue(&q, IID_PPV_ARGS(&h.queue))));
    for (int i = 0; i < Host::kFrames; ++i)
        assert(SUCCEEDED(h.dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&h.alloc[i]))));
    assert(SUCCEEDED(h.dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT,
        h.alloc[0], nullptr, IID_PPV_ARGS(&h.list))));
    assert(SUCCEEDED(h.list->Close()));
    assert(SUCCEEDED(h.dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&h.fence))));
    h.fence_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    assert(BeginCommands());
    const UINT64 first = EndCommands();
    assert(first && WaitFenceValue(h.fence, first, 2000));
    assert(BeginCommands());
    assert(SUCCEEDED(h.list->Close())); // The next Close deterministically fails, no invalid work executes.
    const int slot = h.frame_slot;
    const UINT64 value = h.fence_value, retire = h.alloc_fence[slot];
    const unsigned evaluations = g_eval_count;
    assert(EndCommands() == 0);
    assert(g_submission_failed && h.fence_value == value);
    assert(h.frame_slot == slot && h.alloc_fence[slot] == retire);
    assert(h.fence->GetCompletedValue() == first && g_eval_count == evaluations);
    assert(h.list != nullptr && !BeginCommands());
    FILE *out = tmpfile();
    const char byte = 0;
    assert(out && !WriteExact(out, &byte, 1) && ftell(out) == 0);
    fclose(out);
    // Test-only: abandon the failed request, then verify replacement list usability.
    g_submission_failed = false;
    assert(BeginCommands());
    const UINT64 next = EndCommands();
    assert(next == value + 1 && WaitFenceValue(h.fence, next, 2000));
    assert(g_frame_stamp.fence == next);
    VideoState video;
    VideoFrameHeader frame = {};
    g_frame_stamp = {};
    ProfileFrameResult(video, frame, false, "idle");
    assert(strstr(g_frame_records[0], "fence=0 ") != nullptr);
    FlushProfileFrames();
    for (unsigned i = 0; i < 257; ++i) ProfileFrameResult(video, frame, false, "idle");
    assert(g_frame_record_count == 1); // A full bounded buffer flushes instead of losing identities.
    g_frame_stamp = {};
    { ProfileRequest request{video, frame}; }
    assert(g_frame_stamp.reported && g_frame_record_count == 0);
    return 0;
}
'''.replace('SOURCE', (ROOT / 'native/dlss5-feed-host64.cpp').as_posix()), encoding='utf-8')
        native = ROOT / 'native'
        build = work / 'build.bat'
        build.write_text(f'''@echo off
call "{vcvars}" >nul
if errorlevel 1 exit /b 1
cl /nologo /O2 /EHsc /W3 /MD /std:c++17 /I"{native / 'include'}" /I"{native / 'src'}" "{source}" "{native / 'spout_bridge.cpp'}" /Fe:"{work / 'close_check.exe'}" /link "{native / 'lib/Windows_x86_64/x64/nvsdk_ngx_d.lib'}" "{native / 'SpoutDX.lib'}" version.lib kernel32.lib user32.lib gdi32.lib advapi32.lib ole32.lib d3d11.lib d3d12.lib dxgi.lib d3dcompiler.lib WindowsApp.lib dwmapi.lib
''', encoding='ascii')
        built = subprocess.run([os.environ['COMSPEC'], '/d', '/c', str(build)], cwd=work,
                               capture_output=True, text=True, timeout=120)
        assert built.returncode == 0, built.stdout + built.stderr
        env = dict(os.environ, PATH=str(native) + os.pathsep + os.environ['PATH'])
        checked = subprocess.run([str(work / 'close_check.exe')], cwd=work, env=env,
                                 capture_output=True, text=True, timeout=15)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert 'Close failed' in checked.stdout + checked.stderr
        assert 'result=failure' in checked.stdout + checked.stderr
    print('OK: failed Close never submits, advances, or replies; replacement retires safely')


def check_frames(phase, dda=False):
    import pygame
    os.environ['SDL_VIDEO_WINDOW_POS'] = '0,0'
    pygame.init()
    w, height = 480, 270
    screen = pygame.display.set_mode((w, height), pygame.NOFRAME)
    pygame.display.set_caption('NeuralScreen retirement test')
    hwnd = pygame.display.get_wm_info()['window']
    screen.fill((31, 97, 211))
    pygame.display.flip()
    params = PROFILES['Natural']
    shm = p.SharedFrameBuffer(w, height, w, height)
    camera = None
    if dda:
        import dxcam
        camera = dxcam.create(output_color='RGB')
    with tempfile.TemporaryFile() as err:
        worker = subprocess.Popen([str(WORKER_EXE), '--live'], cwd=WORKER_EXE.parent,
                                  env=dict(os.environ, NS_PHASE=str(phase), NS_NR_SMALL='0'),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        reader = p.WorkerReader(worker, w, height, shm)
        try:
            worker.stdin.write(struct.pack(p.HEADER_FMT, p.VIDEO_MAGIC, w, height, 2, 0, 0, 0,
                int(params['style']), int(params['auto_mask']), int(params['ui_correction']),
                params['intensity'], params['local_tone'], params['local_structure'],
                params['skin_structure'], 0, 0))
            worker.stdin.write(struct.pack(p.SHM_FMT, p.SHM_MAGIC, shm.color_capacity,
                shm.motion_capacity, 0, 0, shm.name.encode('ascii')))
            worker.stdin.flush()
            reader.wait_sack(15)
            shm.negotiated = True
            if dda:
                p.send_dda(worker, w, height)
                reader.wait_dack(15)
            else:
                p.send_wgc(worker, hwnd)
                assert reader.wait_wgak(15) == (w, height)
            shm.open_gray(160, 90)
            p.send_gray(worker, 160, 90, shm.gray_name)
            reader.wait_gak(10)
            p.send_motion_size(worker, 160, 90)
            reader.wait_mack(10)
            p.send_window(worker, w, height)
            reader.wait_wack(10)
            motion_small = np.zeros((90, 160, 2), np.float16)
            motion_full = np.zeros((height, w, 2), np.float16)
            index = 0
            for index in range(70):
                pygame.event.pump()
                color = (31 + index % 7, 97, 211 - index % 7)
                screen.fill(color)
                pygame.draw.rect(screen, (190, 140, 75), (index * 5 % (w-50), 80, 50, 80))
                pygame.display.flip()
                time.sleep(.03)  # A real capture and at least one two-second profiling interval.
                small = index % 2 == 0
                want = index % 13 == 0
                p.send_frame(worker, index, None, motion_small if small else motion_full,
                             index == 0, index, shm, want_pixels=want,
                             motion_small=small, no_color=True)
                pixels = reader.recv(index, 15)
                if pixels is not None:
                    assert pixels.shape == (height, w, 4) and pixels[..., :3].max() > 30
                gray = shm.read_gray()
                assert gray is not None and gray.size == 160 * 90
                if index > 4:
                    assert gray.mean() > 10, 'gray readback stayed empty'
            # Exact bypass content after capture has settled, still paired through the same input slot.
            for color in [(45, 130, 210), (180, 95, 35)]:
                screen.fill(color)
                pygame.display.flip()
                time.sleep(.08)
                reference = np.array(color)
                if camera:
                    raw = camera.grab(region=(0, 0, w, height))
                    assert raw is not None, 'independent DDA reference did not acquire'
                    reference = raw[50:70,50:70,:3]
                matched = False
                for _ in range(4):
                    index += 1
                    p.send_frame(worker, index, None, motion_small, False, index, shm,
                                 want_pixels=True, motion_small=True, no_color=True, bypass=True)
                    pixels = reader.recv(index, 15)
                    if pixels is not None and np.max(np.abs(pixels[50:70, 50:70, :3].astype(int) - reference)) <= 1:
                        matched = True
                        break
                assert matched, 'bypass pixels differ from the captured source'
                if not dda:
                    expected = np.dot(color, [0.299, 0.587, 0.114])
                    assert abs(float(shm.read_gray().mean()) - expected) <= 2, 'gray luminance changed'
            # Recreate the feature while capture stays live, then submit more actual frames.
            p.send_resize(worker, params, w, height, 2, 0, 0, False)
            reader.wait_rack(15)
            for _ in range(6):
                index += 1
                screen.fill((180 + index % 5, 95, 35))
                pygame.display.flip()
                pygame.event.pump()
                time.sleep(.03)
                p.send_frame(worker, index, None, motion_small, True, index, shm,
                             want_pixels=True, motion_small=True, no_color=True)
                assert reader.recv(index, 15) is not None
            p.send_gray(worker, 0, 0, '')
            reader.wait_gak(10)
            for want_pixels in [True, False]:
                index += 1
                screen.fill((40, 90 + index % 20, 190))
                pygame.display.flip()
                time.sleep(.03)
                p.send_frame(worker, index, None, motion_small, False, index, shm,
                             want_pixels=want_pixels, motion_small=True, no_color=True)
                assert (reader.recv(index, 15) is not None) == want_pixels
            worker.stdin.close()
            assert worker.wait(timeout=15) == 0
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=15)
            err.seek(0)
            log = err.read().decode('utf-8', 'replace')
            shm.close()
            if camera: camera.release()
            pygame.quit()
        for marker in ['fence timeout', 'fence wait failed', 'did not retire allocator',
                       'device removed at', 'Close failed', 'tail token order failed']:
            assert marker not in log, marker
        if phase:
            records = [line for line in log.splitlines() if '[phase] frame index=' in line]
            indices = [int(re.search(r' index=(\d+)', line)[1]) for line in records]
            assert indices == list(range(index + 1)), 'EOF lost or duplicated request identities'
            ages = [float(re.search(r' age=([-\d.]+)', line)[1]) for line in records
                    if 'result=enhanced' in line and 'fresh=1' in line]
            assert ages and all(age >= 0 for age in ages), f'missing/negative presented frame age: {records[:3]}'
            if dda:
                assert any('clock=dda-qpc' in line and 'fresh=1' in line for line in records)
            else:
                assert all('clock=acquisition-to-present-call' in line for line in records)
            outstanding = [int(value) for value in re.findall(r'outstanding-max=(\d+)', log)]
            assert outstanding and max(outstanding) >= 2, 'deferred tail was not exercised'
        else:
            assert '[phase] frame' not in log
    print(f'OK: phase={phase}, capture={"DDA" if dda else "WGC"}, active allocator rollover, gray, motion, pixels, bypass, rebuild and EOF')


if __name__ == '__main__':
    check_hdr_transition_guard()
    check_close_failure()
    check_frames(1)
    check_frames(0)
    check_frames(1, dda=True)
