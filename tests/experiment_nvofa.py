"""Isolated WGC -> guides -> NR -> present A/B benchmark.
No changes to user config. Synthetic repeatable scrolling, not a game benchmark.
"""
import ctypes, json, os, struct, subprocess, sys, threading, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2, numpy as np
import pygame
import protocol as wire
from guides import TemporalGuideGenerator
from motion_backend import MotionBackendStatus
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
cv2.setNumThreads(4)
OUT=ROOT/'_work/nvofa-results';OUT.mkdir(parents=True,exist_ok=True)

def exact(p,n):
    b=bytearray()
    while len(b)<n:
        c=p.read(n-len(b))
        if not c:raise RuntimeError('worker EOF')
        b.extend(c)
    return b

def run(mode,quality=False,round_id=0,static=False,failure=False,hdr=True,resize=False):
    w,h=(1280,720) if quality else (2560,1440)
    screen=pygame.display.set_mode((w,h),pygame.NOFRAME)
    rng=np.random.default_rng(123)
    base=cv2.GaussianBlur(rng.integers(0,256,(180,320),dtype=np.uint8),(5,5),0)
    for y in range(18,180,25):cv2.putText(base,'TEXT 123 Test',(12,y),cv2.FONT_HERSHEY_SIMPLEX,.5,230,1)
    surfaces=[]
    for i in range(16):
        gray=np.roll(base,i,axis=1)
        rgb=cv2.cvtColor(cv2.resize(gray,(w,h),interpolation=cv2.INTER_LINEAR),cv2.COLOR_GRAY2RGB)
        surfaces.append(pygame.surfarray.make_surface(rgb.transpose(1,0,2)))
    screen.blit(surfaces[0],(0,0));pygame.display.flip()
    tag=f'{mode}-q{int(quality)}-r{round_id}'+('-static' if static else '')+('-fallback' if failure else '')+('-sdr' if not hdr else '')+('-resize' if resize else '')
    folder=OUT/tag;folder.mkdir(exist_ok=True)
    env=dict(os.environ,NS_HDR='1' if hdr else '0',NS_NR_SMALL='1',NS_MOTION_BACKEND='nvofa' if mode=='nvofa' else 'cpu',
             NS_PHASE_TIMING='1')
    if failure:env['NS_NVOFA_TEST_FAIL_AT']='60'
    else:env.pop('NS_NVOFA_TEST_FAIL_AT',None)
    if quality and mode=='nvofa':env['NS_NVOFA_DUMP']=str(folder)
    else:env.pop('NS_NVOFA_DUMP',None)
    p=subprocess.Popen([str(ROOT/'native/nvngx.dll'),'--live'],cwd=ROOT/'native',env=env,
          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    logs=[];drain=threading.Thread(target=lambda:logs.extend(iter(p.stderr.readline,b'')),daemon=True);drain.start()
    watchdog=threading.Timer(100,p.kill);watchdog.start()
    shm=wire.SharedFrameBuffer(w,h);shm.open_gray(320,180)
    guide=TemporalGuideGenerator(w,h,emit_small=True)
    def ack(fmt):
        a=struct.unpack(fmt,exact(p.stdout,struct.calcsize(fmt)))
        assert a[1 if fmt!=wire.OUT_FMT else 2]==1,a
        return a
    rows=[];quality_rows=[];prev=None
    status=MotionBackendStatus(); hardware_frames=[]
    try:
        p.stdin.write(struct.pack(wire.HEADER_FMT,wire.VIDEO_MAGIC,w,h,8,0,0,0,1,0,0,1.,1.,1.,-1.,w,h));p.stdin.flush()
        wire.send_wgc(p,pygame.display.get_wm_info()['window']);ack(wire.WGC_ACK_FMT)
        wire.send_motion_size(p,320,180);ack(wire.MOTION_ACK_FMT)
        wire.send_gray(p,320,180,shm.gray_name);ack(wire.GRAY_ACK_FMT)
        wire.send_window(p,w,h);ack(wire.WINDOW_ACK_FMT)
        time.sleep(.15)
        for i in range(24 if quality else 220):
            if resize and i in (70,140):
                w,h=(1280,720) if i==70 else (2560,1440)
                params=dict(profile=0,preset=0,style=0,auto_mask=1,ui_correction=0,
                            intensity=1.,local_tone=1.,local_structure=1.,skin_structure=-1.)
                wire.send_resize(p,params,w,h,1,w,h,nr_small=True)
                ack('<4Iq')
                wire.send_motion_size(p,320,180);ack(wire.MOTION_ACK_FMT)
                wire.send_gray(p,320,180,shm.gray_name);ack(wire.GRAY_ACK_FMT)
                guide=TemporalGuideGenerator(w,h,emit_small=True)
            pygame.event.pump();screen.blit(surfaces[0 if static else i%16],(0,0));pygame.display.flip()
            if quality:time.sleep(.025)
            t0=time.perf_counter()
            t1=time.perf_counter();gray=shm.read_gray().copy().reshape(180,320)
            hardware=mode=='nvofa' and status.update(p,[x.decode('utf-8',errors='replace') for x in logs])
            hardware_frames.append(hardware)
            g=guide.process(gray=gray,compute_motion=not hardware)
            t2=time.perf_counter()
            wire.send_frame(p,i,None,g.motion,g.reset,i,no_color=True,motion_small=True,want_pixels=False)
            a=ack(wire.OUT_FMT)
            if a[3]:exact(p.stdout,a[3])
            t3=time.perf_counter()
            if quality and mode == "nvofa": gray=shm.read_gray().copy().reshape(180,320)
            changed=prev is not None and np.mean(cv2.absdiff(gray,prev))>.255
            if i>=40:rows.append({'prepare_ms':(t1-t0)*1000,'guides_ms':(t2-t1)*1000,'worker_ms':(t3-t2)*1000,'total_ms':(t3-t0)*1000,'changed':bool(changed)})
            if quality and i>0:
                # Compare against measured translation of the actual captured pair,
                # rather than assuming WGC delivered the newest drawn source frame.
                candidates=[]
                for dy in range(-2,3):
                    for dx in range(-16,17):
                        shifted=np.roll(prev,(dy,dx),axis=(0,1))
                        error=float(np.mean(cv2.absdiff(shifted,gray)[8:-8,20:-20]))
                        candidates.append((error,dx,dy))
                residual,dx,dy=min(candidates)
                response=1.0-residual/255.0
                target=-np.array([dx,dy],dtype=np.float32)
                if mode=='nvofa':
                    fpath=folder/f'flow-{i:04d}.bin'
                    vectors=cv2.resize(np.fromfile(fpath,np.int16).reshape(45,80,2).astype(np.float32)/32,(320,180),interpolation=cv2.INTER_LINEAR)
                    expanded=np.fromfile(folder/f'motion-{i:04d}.bin',np.float16).reshape(h,w,2).astype(np.float32)
                    expanded=cv2.resize(expanded,(320,180))/np.array([w/320,h/180])
                    assert np.isfinite(expanded).all()
                    if g.reset: assert not expanded.any(), 'reset must zero expanded motion'
                    elif response>.98: assert np.median(np.linalg.norm(expanded[16:-16,24:-24]-target,axis=2))<.5, 'expanded sign/scale mismatch'
                else:
                    vectors=g.motion.astype(np.float32)/np.array([w/320,h/180])
                inner=vectors[16:-16,24:-24]
                err=np.linalg.norm(inner-target,axis=2)
                quality_rows.append({'target':target.tolist(),'phase_response':response,'median_epe':float(np.median(err)),'p95_epe':float(np.percentile(err,95)),'median_vector':np.median(inner,axis=(0,1)).tolist(),'reset':bool(g.reset)})
            prev=gray
        p.stdin.close();p.wait(timeout=15);assert p.returncode==0,p.returncode
    finally:
        if p.poll() is None:p.kill();p.wait()
        watchdog.cancel();drain.join(timeout=2);shm.close()
        print('worker exit',p.returncode,flush=True)
        (folder/'worker.log').write_bytes(b''.join(logs))
    log=b''.join(logs).decode('utf-8',errors='replace')
    if mode=='nvofa':
        assert '[nvofa] active:' in log, log
        if failure:
            assert '[nvofa] unavailable: injected execute failure' in log, log
            assert any(hardware_frames) and not any(hardware_frames[80:]), hardware_frames
        else: assert '[nvofa] unavailable:' not in log, log
    if quality:
        valid=[r for r in quality_rows if not r['reset'] and r['phase_response'] > .98]
        assert len(valid)>=16, quality_rows
        assert max(r['median_epe'] for r in valid)<.5, valid
        assert any(abs(r['target'][0])>=10 for r in valid), 'large displacement not captured'
    result={'mode':mode,'quality':quality,'round':round_id,'static':static,'rows':rows,'quality_rows':quality_rows}
    if rows:
        result['summary']={k:{'median':float(np.median([r[k] for r in rows])),'mean':float(np.mean([r[k] for r in rows])),'p95':float(np.percentile([r[k] for r in rows],95))} for k in ['prepare_ms','guides_ms','worker_ms','total_ms']}
        result['changed_fraction']=float(np.mean([r['changed'] for r in rows]))
    (folder/'result.json').write_text(json.dumps(result,indent=2))
    print(tag,json.dumps(result.get('summary',quality_rows[-3:])),flush=True)
    return result

if __name__=='__main__':
    pygame.init()
    try:
        if '--resize' in sys.argv:
            run('nvofa',resize=True,hdr=False)
        elif '--fallback' in sys.argv:
            run('nvofa',failure=True)
        elif '--sdr' in sys.argv:
            for m in ['cpu','nvofa']:run(m,hdr=False)
        elif '--static' in sys.argv:
            for m in ['cpu','nvofa']:run(m,static=True)
        elif '--quality' in sys.argv:
            for m in ['cpu','nvofa']:run(m,quality=True)
        else:
            for i,m in enumerate(['cpu','nvofa','nvofa','cpu']):run(m,round_id=i)
    finally:pygame.quit()
