"""Opt-in GPU regression: a prepared capture must survive source redraws."""
import ctypes
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import protocol as wire

def exact(pipe,n):
    data=bytearray()
    while len(data)<n:
        part=pipe.read(n-len(data))
        if not part: raise RuntimeError('worker exited')
        data.extend(part)
    return data

def run():
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    import pygame
    pygame.init()
    w,h=640,360
    screen=pygame.display.set_mode((w,h),pygame.NOFRAME)
    screen.fill((210,40,20)); pygame.display.flip()
    hwnd=pygame.display.get_wm_info()['window']
    p=subprocess.Popen([str(ROOT/'native/nvngx.dll'),'--live'],cwd=ROOT/'native',
        env=dict(os.environ,NS_HDR='0',NS_FRAMEGEN='0'),stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    logs=[]
    thread=threading.Thread(target=lambda:logs.extend(iter(p.stderr.readline,b'')),daemon=True);thread.start()
    watchdog=threading.Timer(30,p.kill);watchdog.start()
    def ack(fmt): return struct.unpack(fmt,exact(p.stdout,struct.calcsize(fmt)))
    def capture(index):
        p.stdin.write(struct.pack(wire.FRAME_FMT,wire.CAPTURE_MAGIC,index,0,0,index));p.stdin.flush()
        assert ack(wire.OUT_FMT)[2]==1
    def consume(index):
        wire.send_frame(p,index,None,np.zeros((h,w,2),np.float16),True,index,
            no_color=True,bypass=True,want_pixels=True,prepared=True)
        a=ack(wire.OUT_FMT);assert a[2]==1 and a[3]==w*h*4,a
        return np.frombuffer(exact(p.stdout,a[3]),np.uint8).reshape(h,w,4)
    try:
        p.stdin.write(struct.pack(wire.HEADER_FMT,wire.VIDEO_MAGIC,w,h,1,0,0,0,1,0,0,1.,1.,1.,-1.,w,h));p.stdin.flush()
        wire.send_wgc(p,hwnd);assert ack(wire.WGC_ACK_FMT)[1]==1
        time.sleep(.1)
        capture(0)
        screen.fill((20,40,210));pygame.display.flip();time.sleep(.1)
        red=consume(0)[80:280,80:560,:3].mean((0,1))
        capture(1)
        blue=consume(1)[80:280,80:560,:3].mean((0,1))
        assert red[0]>150 and red[2]<70,red
        assert blue[2]>150 and blue[0]<70,blue
        # A capture cannot be consumed twice or under a different ID.
        wire.send_frame(p,2,None,np.zeros((h,w,2),np.float16),True,2,no_color=True,prepared=True)
        p.stdin.close();p.wait(timeout=10)
        assert p.returncode==10,p.returncode
        print('PASS: exact capture retained across redraw; next capture updates; stale ID rejected')
    finally:
        if p.poll() is None: p.kill();p.wait()
        watchdog.cancel();thread.join(timeout=3);pygame.quit()
    assert b'prepared frame ID mismatch' in b''.join(logs)

if __name__=='__main__':
    if '--run' in sys.argv:run()
    else:print('SKIP: use --run for Windows GPU capture test')
