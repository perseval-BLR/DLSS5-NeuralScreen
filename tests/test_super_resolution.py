"""Opt-in SR output, toggle, bypass and resize regression with real NVIDIA DLLs."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import protocol as wire
from test_prepared_capture import exact

def run():
    w,h=640,360;ow,oh=960,540
    p=subprocess.Popen([str(ROOT/'native/nvngx.dll'),'--live'],cwd=ROOT/'native',
        env=dict(os.environ,NS_NR_SMALL='1',NS_DLSS_SR='0',NS_FRAMEGEN='0'),
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    logs=[];thread=threading.Thread(target=lambda:logs.extend(iter(p.stderr.readline,b'')),daemon=True);thread.start()
    watchdog=threading.Timer(45,p.kill);watchdog.start()
    def ack(fmt):return struct.unpack(fmt,exact(p.stdout,struct.calcsize(fmt)))
    try:
        p.stdin.write(struct.pack(wire.HEADER_FMT,wire.VIDEO_MAGIC,w,h,1,0,0,0,1,0,0,1.,1.,1.,-1.,ow,oh));p.stdin.flush()
        for i in range(80):
            if i in (20,25,30,75):
                scale={20:50,25:25,30:50,75:100}[i]
                p.stdin.write(struct.pack(wire.FRAME_FMT, wire.SR_SCALE_MAGIC, i, scale, 0, i));p.stdin.flush()
                assert ack(wire.OUT_FMT)[1:3] == (i, 1)
            if i in (35,70):
                w,h=(480,270) if i==35 else (ow,oh)
                params=dict(profile=0,preset=0,style=0,auto_mask=1,ui_correction=0,intensity=1.,local_tone=1.,local_structure=1.,skin_structure=-1.)
                wire.send_resize(p,params,w,h,1,ow,oh,nr_small=i!=70)
                a=ack(wire.RACK_FMT);assert a[1]==1,a
            frame=np.full((oh,ow,4),(30,90,150,255),np.uint8)
            frame[100:350,100+i*3:350+i*3,:3]=(210,80,30)
            motion=np.zeros((h,w,2),np.float16);motion[:,:,0]=-3*w/ow
            bypass=45<=i<48
            sr=10<=i<55 or i>=60
            wire.send_frame(p,i,frame,motion,i in (0,35,48,60),i,bypass=bypass,dlss_sr=sr)
            a=ack(wire.OUT_FMT);assert a[2]==1 and a[3]==ow*oh*4,a
            out=np.frombuffer(exact(p.stdout,a[3]),np.uint8).reshape(oh,ow,4)
            if bypass:assert np.array_equal(out,frame),'SR changed bypass'
            assert out[:,:,:3].mean()>20,'black output'
        p.stdin.close();p.wait(timeout=10)
    finally:
        if p.poll() is None:p.kill();p.wait()
        watchdog.cancel();thread.join(timeout=2)
    log=b''.join(logs).decode('utf-8','replace')
    print(log)
    assert p.returncode==0,p.returncode
    assert log.count('[sr] ready:')==8,log
    assert 'reduced 480x270 -> NR 320x180 -> SR input 480x270 -> 960x540' in log
    assert 'reduced 480x270 -> NR 256x144 -> SR input 480x270 -> 960x540' in log
    assert 'reduced 480x270 -> NR 480x270 -> SR input 480x270 -> 960x540' in log
    assert 'reduced 960x540 -> NR 960x540 -> SR input 960x540 -> 960x540' in log
    assert 'input scale: 50% (before NR; Boost ratio unchanged)' in log
    assert log.count('[sr] first evaluation succeeded')>=4,log
    assert '[sr] Evaluate failed' not in log and '[sr] CreateFeature failed' not in log
    print('PASS: reduction before NR, independent Boost ratio, bypass, toggle and resize')

if __name__=='__main__':
    if '--run' in sys.argv:run()
    else:print('SKIP: use --run for the NVIDIA GPU test')
