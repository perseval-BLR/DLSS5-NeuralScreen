"""DLSS checks and staged updates; loaded libraries are never replaced live."""
import hashlib
import json
import shutil
import time
import os
import ctypes
import re
import struct
import threading
import urllib.request

from paths import NATIVE_DIR

UPDATABLE = {'DLSS SR': 'nvngx_dlss.dll', 'DLSS FG': 'nvngx_dlssg.dll'}
INSTALL_ERRORS = set()


def stage_update(filename, expected, directory=NATIVE_DIR):
    """Download official bytes, verify repository blob hash and PE version."""
    if filename not in UPDATABLE.values():
        raise ValueError('Unsupported library')
    url = ('https://api.github.com/repos/NVIDIA/DLSS/contents/'
           'lib/Windows_x86_64/rel/' + filename)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'NeuralScreen-library-update',
        'Accept': 'application/vnd.github.object+json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Oversized metadata')
        meta = json.loads(raw)
    size, sha = meta['size'], meta['sha']
    download = meta['download_url']
    if (meta.get('type') != 'file' or meta.get('name') != filename
            or not isinstance(size, int) or not 4096 <= size <= 100 * 1024 * 1024
            or not re.fullmatch('[0-9a-f]{40}', sha)
            or not download.startswith('https://raw.githubusercontent.com/NVIDIA/DLSS/')
            or not download.endswith('/lib/Windows_x86_64/rel/' + filename)):
        raise ValueError('Invalid official metadata')
    part = directory / (filename + '.part')
    ready = directory / (filename + '.ready')
    manifest = directory / (filename + '.update.json')
    temp_manifest = directory / (filename + '.update.tmp')
    blob = hashlib.sha1(f'blob {size}\0'.encode())
    digest = hashlib.sha256()
    received = 0
    deadline = time.monotonic() + 180
    try:
        with urllib.request.urlopen(urllib.request.Request(download, headers={
                'User-Agent': 'NeuralScreen-library-update'}), timeout=15) as response, part.open('wb') as out:
            if response.status != 200:
                raise ValueError('Incomplete download response')
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > size or time.monotonic() > deadline:
                    raise ValueError('Download exceeded size or time limit')
                blob.update(chunk)
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if received != size or blob.hexdigest() != sha:
            raise ValueError('Downloaded bytes do not match official repository')
        if local_version(part) != expected:
            raise ValueError('Published version changed; check versions again')
        os.replace(part, ready)
        temp_manifest.write_text(json.dumps({'version': expected, 'sha256': digest.hexdigest()}), encoding='utf-8')
        os.replace(temp_manifest, manifest)
    finally:
        part.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)


def apply_pending(directory=NATIVE_DIR):
    """Called before the first worker starts. Keep an original .bak on success."""
    for filename in UPDATABLE.values():
        manifest = directory / (filename + '.update.json')
        if not manifest.exists():
            continue
        ready = directory / (filename + '.ready')
        target = directory / filename
        try:
            if manifest.stat().st_size > 4096 or ready.stat().st_size > 100 * 1024 * 1024:
                raise ValueError('Oversized staged update')
            meta = json.loads(manifest.read_text(encoding='utf-8'))
            with ready.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            version = local_version(ready)
            if digest != meta['sha256'] or list(version) != meta['version']:
                raise ValueError('Staged update failed integrity check')
            if target.exists():
                if local_version(target) >= version:
                    manifest.unlink()
                    ready.unlink()
                    continue
                shutil.copy2(target, directory / (filename + '.bak'))
            os.replace(ready, target)
            manifest.unlink()
            print(f'[libraries] installed {filename} {version_text(version)}')
            INSTALL_ERRORS.discard(filename)
        except Exception as exc:
            # A running second instance may lock the DLL: retain the update for retry.
            print(f'[libraries] could not install {filename}: {exc}')
            INSTALL_ERRORS.add(filename)


def file_version(read):
    """Extract fixed file version from a PE resource section without loading code."""
    header = read(0, 4096)
    if header[:2] != b'MZ':
        raise ValueError('Not a PE file')
    pe = struct.unpack_from('<I', header, 60)[0]
    if pe > 3072 or header[pe:pe + 4] != b'PE\0\0':
        raise ValueError('Invalid PE header')
    count = struct.unpack_from('<H', header, pe + 6)[0]
    optional = struct.unpack_from('<H', header, pe + 20)[0]
    table = pe + 24 + optional
    if not 0 < count <= 32 or table + count * 40 > len(header):
        raise ValueError('Invalid section table')
    for index in range(count):
        pos = table + index * 40
        if header[pos:pos + 8].rstrip(b'\0') != b'.rsrc':
            continue
        size, offset = struct.unpack_from('<II', header, pos + 16)
        if not 0 < size <= 1024 * 1024 or offset < 4096:
            raise ValueError('Invalid resource size')
        resource = read(offset, size)
        key = 'VS_VERSION_INFO\0'.encode('utf-16le')
        start = resource.find(key)
        if start < 6:
            raise ValueError('Missing version resource')
        fixed = (start + len(key) + 3) & ~3
        signature, version, ms, ls = struct.unpack_from('<IIII', resource, fixed)
        if signature != 0xFEEF04BD or version != 0x10000:
            raise ValueError('Invalid fixed version')
        return (ms >> 16, ms & 65535, ls >> 16, ls & 65535)
    raise ValueError('No resource section')


def local_version(path):
    # VERSION APIs read resources without loading/executing the library.
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    api = ctypes.WinDLL('version', use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    api.GetFileVersionInfoSizeW.restype = ctypes.c_uint32
    api.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                      ctypes.c_uint32, ctypes.c_void_p]
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                 ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.POINTER(ctypes.c_uint32)]
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not 0 < size <= 1024 * 1024:
        raise ValueError('Missing or oversized version metadata')
    data = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, data):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), ctypes.c_uint32()
    if not api.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 52:
        raise ValueError('Missing fixed version')
    signature, version, ms, ls = struct.unpack('<IIII', ctypes.string_at(pointer, 16))
    if signature != 0xFEEF04BD or version != 0x10000:
        raise ValueError('Invalid fixed version')
    return (ms >> 16, ms & 65535, ls >> 16, ls & 65535)


def remote_version(filename):
    url = ('https://raw.githubusercontent.com/NVIDIA/DLSS/main/'
           'lib/Windows_x86_64/rel/' + filename)
    etag = None

    def read(offset, size):
        nonlocal etag
        request = urllib.request.Request(url, headers={
            'User-Agent': 'NeuralScreen-library-check',
            'Range': f'bytes={offset}-{offset + size - 1}',
            'Cache-Control': 'no-cache',
        })
        with urllib.request.urlopen(request, timeout=10) as response:
            content_range = response.headers.get('Content-Range', '')
            if response.status != 206 or not re.fullmatch(
                    rf'bytes {offset}-{offset + size - 1}/\d+', content_range):
                raise ValueError('Server did not honor bounded range request')
            current = response.headers.get('ETag')
            if not current or (etag is not None and current != etag):
                raise ValueError('Remote library changed during check')
            etag = current
            data = response.read(size + 1)
            if len(data) != size:
                raise ValueError('Truncated or oversized range response')
            return data
    return file_version(read)


def version_text(version):
    return '.'.join(map(str, version)) if version else '?'


class LibraryChecker:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = False
        self.rows = ()
        self.notice_shown = False

    def take_notice(self):
        with self.lock:
            if self.notice_shown or self.busy or not any(r[3] == 'update' for r in self.rows):
                return False
            self.notice_shown = True
            return True

    def update_all(self):
        with self.lock:
            if self.busy:
                return False
            todo = [(r[0], tuple(map(int, r[2].split('.')))) for r in self.rows
                    if r[0] in UPDATABLE and r[3] in ('update', 'download_failed', 'install_failed') and r[2] != '?']
            if not todo:
                return False
            self.busy = True
        threading.Thread(target=self._batch, args=(todo,), name='library-download', daemon=True).start()
        return True

    def _batch(self, todo):
        try:
            for label, expected in todo:
                with self.lock:
                    self.rows = tuple((*r[:3], 'downloading') if r[0] == label else r for r in self.rows)
                self._download(label, expected, finish=False)
        finally:
            with self.lock:
                self.busy = False

    def snapshot(self):
        with self.lock:
            return self.busy, self.rows

    def start(self):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        threading.Thread(target=self._run, name='library-updates', daemon=True).start()
        return True

    def update(self, label):
        with self.lock:
            row = next((r for r in self.rows if r[0] == label), None)
            if self.busy or label not in UPDATABLE or not row or row[3] not in ('update', 'download_failed', 'install_failed') or row[2] == '?':
                return False
            expected = tuple(map(int, row[2].split('.')))
            self.busy = True
            self.rows = tuple((*r[:3], 'downloading') if r[0] == label else r for r in self.rows)
        threading.Thread(target=self._download, args=(label, expected),
                         name='library-download', daemon=True).start()
        return True

    def _download(self, label, expected, finish=True):
        status = 'pending'
        try:
            stage_update(UPDATABLE[label], expected)
            print(f'[libraries] {label}: update ready; restart to install')
        except Exception as exc:
            status = 'download_failed'
            print(f'[libraries] {label}: download failed: {exc}')
        finally:
            with self.lock:
                self.rows = tuple((*r[:3], status) if r[0] == label else r for r in self.rows)
                if finish:
                    self.busy = False

    def _run(self):
        rows = []
        try:
            for label, filename in [('DLSS SR', 'nvngx_dlss.dll'),
                                    ('DLSS FG', 'nvngx_dlssg.dll'),
                                    ('DLSS NR', 'nvngx_dlssnr.dll')]:
                path = (os.environ.get('NS_NR_DLL') if label == 'DLSS NR' else None)
                path = path or NATIVE_DIR / filename
                installed = latest = None
                try:
                    installed = local_version(path)
                    status = 'no_source' if label == 'DLSS NR' else 'unknown'
                except FileNotFoundError:
                    status = 'missing'
                except Exception as exc:
                    status = 'unknown'
                    print(f'[libraries] {label}: local version unavailable: {exc}')
                if label != 'DLSS NR':
                    try:
                        latest = remote_version(filename)
                        if installed:
                            status = ('update' if latest > installed else
                                      'newer' if installed > latest else 'current')
                    except Exception as exc:
                        print(f'[libraries] {label}: official version unavailable: {exc}')
                if label in UPDATABLE and (NATIVE_DIR / (filename + '.update.json')).exists():
                    status = 'install_failed' if filename in INSTALL_ERRORS else 'pending'
                rows.append((label, version_text(installed), version_text(latest), status))
                print(f'[libraries] {label}: installed={version_text(installed)}, '
                      f'official={version_text(latest)}, status={status}')
        finally:
            with self.lock:
                self.rows = tuple(rows)
                self.busy = False


checker = LibraryChecker()
