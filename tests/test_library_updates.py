"""Version checks never replace DLLs or report an offline check as current."""
import os
from pathlib import Path
import struct
import sys
import threading
import tempfile
import hashlib
import json
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import library_updates as updates


class Checks(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.create_connection', side_effect=AssertionError('Tests must stay offline'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_startup_is_offline_by_default(self):
        import startup
        with patch.object(updates.checker, 'start') as start:
            for cfg in ({}, {'library_updates_enabled': False}, {'library_updates_enabled': 'true'}):
                startup._start_library_checks(cfg)
            start.assert_not_called()
            startup._start_library_checks({'library_updates_enabled': True})
            start.assert_called_once()
    def test_notice_once_and_batch(self):
        checker = updates.LibraryChecker()
        self.assertFalse(checker.take_notice())
        checker.rows = (('DLSS SR', '310.8.0.0', '310.9.1.0', 'update'),
                        ('DLSS FG', '310.8.0.0', '310.9.1.0', 'update'))
        checker.busy = True
        self.assertFalse(checker.take_notice())
        checker.busy = False
        self.assertTrue(checker.take_notice())
        self.assertFalse(checker.take_notice())
        with patch.object(updates, 'stage_update') as download:
            self.assertTrue(checker.update_all())
            for thread in threading.enumerate():
                if thread.name == 'library-download':
                    thread.join(3)
        self.assertEqual(download.call_count, 2)
        self.assertFalse(checker.snapshot()[0])
        self.assertTrue(all(r[3] == 'pending' for r in checker.snapshot()[1]))

    def test_notice_buttons(self):
        os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
        import pygame
        from test_ui_buttons import build, paint, find, click
        import commands
        from types import SimpleNamespace
        pygame.init()
        try:
            menu = build()
            menu.page = 'updates'
            menu.set_state({'library_updates': (False, (
                ('DLSS SR', '310.8.0.0', '310.9.1.0', 'update'),))})
            paint(menu)
            button = find(menu, 'button', 'update_libraries')
            self.assertIsNotNone(button)
            self.assertEqual(click(menu, button), [('button', 'update_libraries')])
            with patch.object(commands.library_checker, 'update_all') as update:
                commands.apply_menu_action(None, ('button', 'update_libraries'))
                update.assert_called_once()
            self.assertIsNotNone(find(menu, 'button', 'close'))
            self.assertIsNone(find(menu, 'action', 'exit'))
            state = SimpleNamespace(display=SimpleNamespace(menu=menu,
                set_menu_opaque=lambda _: None, set_menu_input=lambda _: None))
            with patch.object(commands.settings_io, 'save_menu_layout'):
                commands.apply_menu_action(state, ('button', 'close'))
            self.assertFalse(menu.visible)
            self.assertEqual(menu.page, 'main')
        finally:
            pygame.quit()

    def test_install_and_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            target = directory / 'nvngx_dlss.dll'
            ready = directory / 'nvngx_dlss.dll.ready'
            manifest = directory / 'nvngx_dlss.dll.update.json'
            target.write_bytes(b'old')
            ready.write_bytes(b'new')
            manifest.write_text(json.dumps({'version': [310, 10, 0, 0],
                'sha256': hashlib.sha256(b'new').hexdigest()}))
            def version(path):
                return (310, 10 if Path(path).read_bytes() == b'new' else 9, 0, 0)
            with patch.object(updates, 'local_version', side_effect=version):
                updates.apply_pending(directory)
            self.assertEqual(target.read_bytes(), b'new')
            self.assertEqual((directory / 'nvngx_dlss.dll.bak').read_bytes(), b'old')
            self.assertFalse(manifest.exists())
            ready.write_bytes(b'corrupt')
            manifest.write_text(json.dumps({'version': [310, 11, 0, 0], 'sha256': 'wrong'}))
            with patch.object(updates, 'local_version', return_value=(310, 11, 0, 0)):
                updates.apply_pending(directory)
            self.assertEqual(target.read_bytes(), b'new')
            self.assertTrue(manifest.exists())

    def test_download_failure_can_retry(self):
        checker = updates.LibraryChecker()
        checker.rows = (('DLSS SR', '310.8.0.0', '310.9.1.0', 'update'),)
        with patch.object(updates, 'stage_update', side_effect=OSError('offline')):
            checker._download('DLSS SR', (310, 9, 1, 0))
        self.assertEqual(checker.snapshot()[1][0][3], 'download_failed')
        self.assertFalse(checker.snapshot()[0])
        self.assertFalse(checker.update('DLSS NR'))
        with patch.object(updates, 'stage_update'):
            checker._download('DLSS SR', (310, 9, 1, 0))
        self.assertEqual(checker.snapshot()[1][0][3], 'pending')
        self.assertFalse(checker.update('DLSS SR'))

    def test_pe_metadata(self):
        data = bytearray(8192)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 60, 128)
        data[128:132] = b'PE\0\0'
        struct.pack_into('<H', data, 134, 1)
        data[152:160] = b'.rsrc\0\0\0'
        struct.pack_into('<II', data, 168, 4096, 4096)
        key = 'VS_VERSION_INFO\0'.encode('utf-16le')
        data[4102:4102 + len(key)] = key
        struct.pack_into('<IIII', data, 4136, 0xFEEF04BD, 0x10000,
                         (310 << 16) | 10, 1 << 16)
        self.assertEqual(updates.file_version(lambda o, n: data[o:o+n]), (310, 10, 1, 0))
        struct.pack_into('<I', data, 168, 2**30)
        with self.assertRaises(ValueError):
            updates.file_version(lambda o, n: data[o:o+n])
        with self.assertRaises(ValueError):
            updates.file_version(lambda o, n: b'bad')

    def test_status_and_retry(self):
        checker = updates.LibraryChecker()
        with patch.object(updates, 'local_version', return_value=(310, 9, 1, 0)), \
             patch.object(updates, 'remote_version', side_effect=[(310, 10, 0, 0), (310, 8, 0, 0)]):
            checker._run()
        self.assertEqual([r[3] for r in checker.snapshot()[1]], ['update', 'newer', 'no_source'])
        with patch.object(updates, 'local_version', return_value=(310, 9, 1, 0)), \
             patch.object(updates, 'remote_version', side_effect=OSError('offline')):
            checker._run()
        self.assertEqual([r[3] for r in checker.snapshot()[1]], ['unknown', 'unknown', 'no_source'])
        self.assertFalse(checker.snapshot()[0])

    def test_only_one_background_job(self):
        checker = updates.LibraryChecker()
        entered, release = threading.Event(), threading.Event()
        def remote(_):
            entered.set()
            release.wait(5)
            return (310, 9, 1, 0)
        with patch.object(updates, 'local_version', return_value=(310, 9, 1, 0)), \
             patch.object(updates, 'remote_version', side_effect=remote):
            try:
                self.assertTrue(checker.start())
                self.assertTrue(entered.wait(2))
                self.assertTrue(checker.snapshot()[0])
                self.assertFalse(checker.start())
            finally:
                release.set()
                for thread in threading.enumerate():
                    if thread.name == 'library-updates':
                        thread.join(3)
        self.assertFalse(checker.snapshot()[0])

    def test_unbounded_server_response_rejected(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        with patch.object(updates.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(ValueError):
                updates.remote_version('nvngx_dlss.dll')
        response.read.assert_not_called()

    def test_settings_rows(self):
        os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
        import pygame
        from test_ui_buttons import build, paint, find
        pygame.init()
        try:
            menu = build()
            menu.page = 'settings'
            menu.set_state({'lang': 'ru', 'library_updates': (False, (
                ('DLSS SR', '310.8.0.0', '310.9.1.0', 'update'),
                ('DLSS NR', '310.8.0.0', '?', 'no_source')))})
            paint(menu)
            self.assertIsNotNone(find(menu, 'button', 'check_libraries'))
            self.assertIsNotNone(find(menu, 'button', 'update_library:DLSS SR'))
            self.assertIsNone(find(menu, 'button', 'update_library:DLSS NR'))
            self.assertEqual(find(menu, 'info', 'DLSS SRversion').extra['value'],
                             '310.8.0.0 → 310.9.1.0')
            self.assertEqual(find(menu, 'info', 'DLSS NRstatus').extra['label'],
                             'Нет публичного источника обновлений')
        finally:
            pygame.quit()


if __name__ == '__main__':
    unittest.main()
