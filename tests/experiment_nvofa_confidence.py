"""Opt-in, real NVOFA cost calibration on captured window-over-text sequences.

Run --run on Windows/NVIDIA. Writes raw captures/flow/cost and metrics below
_work/nvofa-confidence. No user config changes. The threshold sweep is offline:
it measures vector rejection, not rendered quality or runtime performance.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
import protocol as wire
from guides import TemporalGuideGenerator
from test_prepared_capture import exact

GW, GH = 320, 180
W, H = 640, 360
RUN_ID = str(time.time_ns())
THRESHOLDS = (0, 1, 2, 4, 8, 16, 32, 64, 128, 255)


def scene(position, seed=11, textured=True):
    rng = np.random.default_rng(seed)
    background = np.full((GH, GW), 45, np.uint8)
    for y in range(14, GH, 18):
        cv2.putText(background, 'TEXT abc 123 / home/test', (4, y),
                    cv2.FONT_HERSHEY_SIMPLEX, .38, 225, 1)
    window = np.full((96, 110), 115, np.uint8)
    if textured:
        window = cv2.GaussianBlur(rng.integers(65, 180, window.shape, np.uint8), (3, 3), 0)
    cv2.rectangle(window, (0, 0), (109, 95), 240, 2)
    cv2.putText(window, 'WINDOW', (8, 22), cv2.FONT_HERSHEY_SIMPLEX, .5, 245, 1)
    x, y = position
    background[y:y+96, x:x+110] = window
    return background


def regions(previous, current):
    """Current->previous truth: window translation, static background, disocclusion.

    Newly exposed background has no visible predecessor; EPE is undefined there.
    Report its spurious nonzero motion separately. Window front edge DOES have
    a predecessor on the window, so it remains in the valid moving mask.
    """
    yy, xx = np.indices((GH, GW))
    def inside(pos):
        x, y = pos
        return (xx >= x) & (xx < x+110) & (yy >= y) & (yy < y+96)
    before, now = inside(previous), inside(current)
    truth = np.zeros((GH, GW, 2), np.float32)
    truth[now] = np.subtract(previous, current)
    # Exclude 2 gray pixels around object boundaries: capture/flow interpolation.
    kernel = np.ones((5, 5), np.uint8)
    moving = cv2.erode(now.astype(np.uint8), kernel).astype(bool)
    static = cv2.erode((~(before | now)).astype(np.uint8), kernel).astype(bool)
    revealed = before & ~now
    return truth, {'moving': moving, 'static': static, 'revealed': revealed}


def expand_cost(cost):
    # Largest cost among the four bilinear flow taps. Reject a contaminated
    # vector rather than interpolating a rejected neighbour into a good one.
    yy, xx = np.indices((GH, GW), dtype=np.float32)
    qx, qy = (xx+.5)/4-.5, (yy+.5)/4-.5
    ax, ay = np.floor(qx).astype(int), np.floor(qy).astype(int)
    return np.maximum.reduce([cost[np.clip(ay+dy, 0, cost.shape[0]-1),
                                          np.clip(ax+dx, 0, cost.shape[1]-1)]
                              for dy in (0, 1) for dx in (0, 1)])


def metrics(flow, truth, masks):
    epe = np.linalg.norm(flow-truth, axis=2)
    result = {}
    for name, mask in masks.items():
        if not mask.any():
            continue
        if name != 'revealed':
            result[name+'_epe'] = float(epe[mask].mean())
            result[name+'_bad1'] = float((epe[mask] > 1).mean())
        result[name+'_nonzero'] = float((np.linalg.norm(flow[mask], axis=1) > .25).mean())
    return result


def run_case(name, positions, textured=True):
    import pygame
    output = ROOT / '_work/nvofa-confidence' / RUN_ID / name
    output.mkdir(parents=True, exist_ok=False)
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    frames = [scene(p, textured=textured) for p in positions]
    references = np.stack(frames).astype(np.float32)
    def show(frame):
        rgb = cv2.cvtColor(cv2.resize(frame, (W, H), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2RGB)
        screen.blit(pygame.surfarray.make_surface(rgb.transpose(1, 0, 2)), (0, 0))
        pygame.event.pump()
        pygame.display.flip()
    show(frames[0])
    env = dict(os.environ, NS_HDR='0', NS_FRAMEGEN='0', NS_NR_SMALL='0',
               NS_MOTION_BACKEND='nvofa', NS_NVOFA_DUMP=str(output))
    env.pop('NS_NVOFA_TEST_FAIL_AT', None)
    p = subprocess.Popen([str(ROOT/'native/nvngx.dll'), '--live'], cwd=ROOT/'native', env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    logs = []
    thread = threading.Thread(target=lambda: logs.extend(iter(p.stderr.readline, b'')), daemon=True)
    thread.start()
    watchdog = threading.Timer(100, p.kill)
    watchdog.start()
    shm = wire.SharedFrameBuffer(W, H)
    shm.open_gray(GW, GH)
    cpu = TemporalGuideGenerator(W, H, emit_small=True)
    rows = []
    def ack(fmt):
        value = struct.unpack(fmt, exact(p.stdout, struct.calcsize(fmt)))
        assert value[2 if fmt == wire.OUT_FMT else 1] == 1, value
        return value
    try:
        p.stdin.write(struct.pack(wire.HEADER_FMT, wire.VIDEO_MAGIC, W, H, 1,
                                 0, 0, 0, 1, 0, 0, 1., 1., 1., -1., W, H))
        p.stdin.flush()
        wire.send_wgc(p, pygame.display.get_wm_info()['window']); ack(wire.WGC_ACK_FMT)
        wire.send_motion_size(p, GW, GH); ack(wire.MOTION_ACK_FMT)
        wire.send_gray(p, GW, GH, shm.gray_name); ack(wire.GRAY_ACK_FMT)
        time.sleep(.15)
        for index, frame in enumerate(frames):
            show(frame)
            time.sleep(.08)  # settle WGC; capture is verified below, never assumed
            for attempt in range(20):
                p.stdin.write(struct.pack(wire.FRAME_FMT, wire.CAPTURE_MAGIC, index, 0, 0, index))
                p.stdin.flush(); ack(wire.OUT_FMT)
                gray = shm.read_gray().copy().reshape(GH, GW)
                # A stale/incorrect capture invalidates ground truth. Drain queued
                # WGC frames without running NVOFA or advancing its input history.
                error = float(np.abs(gray.astype(float)-frame).mean())
                best_error = float(np.abs(references-gray).mean((1, 2)).min())
                if error < 3. and error <= best_error+.05:
                    break
                time.sleep(.025)
            assert error < 3. and error <= best_error+.05, (name, index, error, best_error)
            gray.tofile(output/f'gray-{index:04d}.bin')
            guide = cpu.process(gray=gray)
            dis = guide.motion.astype(np.float32).reshape(GH, GW, 2)/2
            wire.send_frame(p, index, None, guide.motion, index == 0, index,
                            no_color=True, motion_small=True, want_pixels=False, prepared=True)
            value = ack(wire.OUT_FMT)
            if value[3]: exact(p.stdout, value[3])
            motion_file = output/f'motion-{index:04d}.bin'
            assert motion_file.exists(), 'NVOFA did not run; inspect worker.log'
            # Sample production expansion at gray pixel centres (2x2 average).
            mv = np.fromfile(motion_file, np.float16).reshape(H, W, 2).astype(np.float32)
            raw = mv.reshape(GH, 2, GW, 2, 2).mean((1, 3))/2
            cost = np.fromfile(output/f'cost-{index:04d}.bin', np.uint8).reshape(GH//4, GW//4)
            if index == 0:
                assert not mv.any(), 'reset must emit zero motion'
                continue
            truth, masks = regions(positions[index-1], positions[index])
            expanded_cost = expand_cost(cost)
            row = dict(index=index, capture_mae=error, capture_best_mae=best_error, cpu_reset=bool(guide.reset),
                       cost_percentiles=np.percentile(cost, [0, 50, 90, 99, 100]).tolist(),
                       raw=metrics(raw, truth, masks), cpu=metrics(dis, truth, masks), filtered={})
            # Cost == 0 is tested too. Disabled/unfiltered is represented by raw.
            for threshold in THRESHOLDS:
                filtered = raw.copy()
                rejected = expanded_cost > threshold
                filtered[rejected] = 0
                item = metrics(filtered, truth, masks)
                item['rejected'] = float(rejected.mean())
                valid = masks['moving'] | masks['static']
                bad = np.linalg.norm(raw-truth, axis=2) > 1
                item['bad_recall'] = float((rejected & bad & valid).sum()/max(1, (bad & valid).sum()))
                item['good_rejected'] = float((rejected & ~bad & valid).sum()/max(1, (~bad & valid).sum()))
                row['filtered'][str(threshold)] = item
            rows.append(row)
        p.stdin.close(); p.wait(timeout=10)
        assert p.returncode == 0, p.returncode
    finally:
        if p.poll() is None: p.kill(); p.wait()
        watchdog.cancel(); thread.join(timeout=3); shm.close()
        (output/'worker.log').write_bytes(b''.join(logs))
    (output/'metrics.json').write_text(json.dumps(rows, indent=2))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if not args.run:
        parser.error('requires --run and a Windows NVIDIA GPU desktop')
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    import pygame
    pygame.init()
    cv2.setNumThreads(4)
    cases = {
        'calibration-textured': ([(40+i*4, 42) for i in range(18)], True),
        'holdout-flat': ([(130-i*4, 42) for i in range(18)], False),
        'holdout-fast': ([(40+i*12, 42) for i in range(12)], True),
        'holdout-diagonal': ([(40+i*4, 30+i*2) for i in range(18)], True),
        'holdout-static': ([(80, 42)]*10, True),
    }
    result = {}
    try:
        for name, (positions, textured) in cases.items():
            rows = run_case(name, positions, textured)
            result[name] = rows
            print(name, 'PASS:', len(rows), 'captured pairs', flush=True)
    finally:
        pygame.quit()
    (ROOT/'_work/nvofa-confidence'/RUN_ID/'results.json').write_text(json.dumps(result, indent=2))
    (ROOT/'_work/nvofa-confidence/results.json').write_text(json.dumps(result, indent=2))
    print('Artifacts:', ROOT/'_work/nvofa-confidence'/RUN_ID)


if __name__ == '__main__':
    main()
