"""Execute the production send block: one prepared capture, one guide update.

No GPU needed. The worker/guide boundaries are mocked; the actual main-loop
statements are compiled from main.py so duplicate calls cannot hide in a helper.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    tree = ast.parse((ROOT/'main.py').read_text(encoding='utf-8'))
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
              and node.body and isinstance(node.body[0], ast.Expr)
              and isinstance(node.body[0].value, ast.Call)
              and isinstance(node.body[0].value.func, ast.Name)
              and node.body[0].value.func.id == 'check_worker']
    assert len(blocks) == 1, 'worker/guide send path runs twice per frame'
    loop = ast.For(target=ast.Name(id='_frame', ctx=ast.Store()),
                   iter=ast.List(elts=[ast.Constant(0)], ctx=ast.Load()),
                   body=blocks, orelse=[])
    code = compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                   'production-send-block', 'exec')
    for hardware, bypass in ((False, False), (True, False), (True, True)):
        calls = []
        frame = object()
        guide = SimpleNamespace(motion=object(), reset=False)
        def capture(*args):
            calls.append('capture')
        def process(*args, **kwargs):
            calls.append('guides')
            assert calls == ['capture', 'guides']
            assert kwargs['gray'] is frame
            assert kwargs['compute_motion'] is not hardware
            return guide
        guides = SimpleNamespace(process=Mock(side_effect=process),
                                 zero_guide=Mock(return_value=guide), previous_gray=object())
        worker = object()
        st = SimpleNamespace(worker=worker, worker_logs=[], reader=object(),
             cfg={'motion_backend': 'nvofa'}, gray_active=True, frame_index=7, pts=70,
             guides=guides, shm=SimpleNamespace(read_gray=lambda: frame), work_frame=None,
             pending_shot=None, recorder=None, motion_small=True, dda_mode=True,
             split_pos=0, lang='en', guide_fails=0)
        status = SimpleNamespace(failed=False, worker=worker, update=lambda *a: hardware)
        send = Mock()
        namespace = dict(st=st, bypass=bypass, motion_status=status, time=time, sys=sys,
                         check_worker=Mock(), prepare_capture=capture, send_frame=send,
                         _perf=Mock())
        exec(code, namespace)
        assert calls == (['capture'] if bypass else ['capture', 'guides'])
        assert send.call_count == 1
        assert send.call_args.kwargs['prepared'] is True
        assert guides.process.call_count == (0 if bypass else 1)
        if bypass:
            assert guides.previous_gray is None
            guides.zero_guide.assert_called_once()
    print('PASS: capture precedes one guide update; NVOFA skips DIS; bypass skips guides')


if __name__ == '__main__':
    main()
