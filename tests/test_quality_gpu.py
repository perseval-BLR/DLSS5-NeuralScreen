"""Compile and run the production scaling/sharpening shader regression on WARP."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run():
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    install = subprocess.check_output([str(vswhere), '-latest', '-products', '*',
        '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
        '-property', 'installationPath'], text=True).strip()
    if not install:
        raise RuntimeError('MSVC x64 build tools required for shader tests')
    vcvars = Path(install) / 'VC/Auxiliary/Build/vcvars64.bat'
    (ROOT / '_work').mkdir(exist_ok=True)
    command = (f'"{vcvars}" >nul && cl /nologo /EHsc /std:c++17 tests\\quality_gpu.cpp '
               '/Fe:_work\\quality_gpu.exe /Fo:_work\\quality_gpu.obj '
               '/link d3d11.lib d3dcompiler.lib user32.lib')
    subprocess.run('cmd /d /s /c "' + command + '"', cwd=ROOT, check=True)
    subprocess.run([str(ROOT / '_work/quality_gpu.exe')], cwd=ROOT, check=True)


if __name__ == '__main__':
    if '--run' in sys.argv:
        run()
    else:
        print('SKIP: use --run for the compiled WARP shader tests')
