"""Offline regression tests; fixtures contain no real browser credentials."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

import har_to_session
from ydisk_auth import DEFAULT_STATE, cookie_jar, normalize_cookies
from ydisk_saver import DiskWeb, copy_remote, normalize_remote


PHOTO = '/photounlim/2026-10-10 19-28-15.JPG'
COOKIE = {'name': 'test', 'value': 'fake', 'domain': '.yandex.ru',
          'path': '/', 'secure': True, 'expirationDate': 4102444800.5,
          'hostOnly': False, 'httpOnly': True, 'session': False}


class AuthTests(unittest.TestCase):
    def test_extension_and_storage_state(self):
        state = normalize_cookies([COOKIE])
        self.assertEqual(state['cookies'][0]['expires'], 4102444800)
        self.assertEqual(normalize_cookies(state), state)

    def test_session_cookie_and_other_domains(self):
        state = normalize_cookies([
            {**COOKIE, 'session': True},
            {**COOKIE, 'domain': '.other.example'},
            {**COOKIE, 'domain': 'evilyandex.ru'},
        ])
        self.assertEqual(len(state['cookies']), 1)
        self.assertEqual(state['cookies'][0]['expires'], -1)

    def test_legacy_cookies(self):
        state = normalize_cookies({'cookies': [
            {'name': 'test', 'value': '', 'domain': 'disk.yandex.ru', 'expires': -1}
        ]})
        self.assertEqual(state['cookies'][0]['value'], '')
        self.assertEqual(state['cookies'][0]['expires'], -1)

    def test_invalid_exports(self):
        for document in ({}, [], [None], [COOKIE | {'value': None}],
                         [COOKIE | {'expirationDate': 'invalid'}],
                         [COOKIE | {'expirationDate': float('inf')}]):
            with self.subTest(document=document), self.assertRaises(ValueError):
                normalize_cookies(document)

    def test_cookie_header_uses_domain_path_secure_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.json'
            path.write_text(json.dumps([
                COOKIE,
                COOKIE | {'name': 'disk_only', 'domain': 'disk.yandex.ru', 'hostOnly': True},
                COOKIE | {'name': 'expired', 'expirationDate': 1},
                COOKIE | {'name': 'wrong_path', 'path': '/unrelated'},
            ]))
            jar = cookie_jar(path)
            request = Request('https://disk.yandex.ru/client/disk')
            jar.add_cookie_header(request)
            self.assertEqual(request.get_header('Cookie'), 'test=fake; disk_only=fake')
            insecure = Request('http://disk.yandex.ru/client/disk')
            jar.add_cookie_header(insecure)
            self.assertIsNone(insecure.get_header('Cookie'))
            other = Request('https://example.com/')
            jar.add_cookie_header(other)
            self.assertIsNone(other.get_header('Cookie'))
            subdomain = Request('https://sub.disk.yandex.ru/')
            jar.add_cookie_header(subdomain)
            self.assertEqual(subdomain.get_header('Cookie'), 'test=fake')

    def test_converter_local_default_and_private_permissions(self):
        self.assertEqual(DEFAULT_STATE, Path('session.json'))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'cookies.json'
            source.write_text(json.dumps([COOKIE]))
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch('sys.argv', ['har_to_session.py', str(source)]), redirect_stdout(io.StringIO()):
                    self.assertEqual(har_to_session.main(), 0)
                target = Path('session.json')
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertEqual(len(cookie_jar(target)), 1)
            finally:
                os.chdir(previous)

    def test_har_still_converts(self):
        har = {'log': {'entries': [{'request': {
            'url': 'https://disk.yandex.ru/models-v2',
            'headers': [{'name': 'Cookie', 'value': 'test=a=b; other=c'}],
        }}]}}
        state = normalize_cookies(har_to_session.convert_har(har))
        self.assertEqual([(c['name'], c['value']) for c in state['cookies']],
                         [('test', 'a=b'), ('other', 'c')])


class PathTests(unittest.TestCase):
    def test_selected_photo_url(self):
        url = ('https://disk.yandex.ru/client/photo?view=icons'
               '&idDialog=%2Fphotounlim%2F2026-10-10%2019-28-15.JPG')
        self.assertEqual(normalize_remote(url), PHOTO)

    def test_paths_and_folders(self):
        cases = {'/disk/Фото/': '/disk/Фото', 'disk:/Фото': '/disk/Фото',
                 'https://disk.yandex.ru/client/disk/%D0%A4%D0%BE%D1%82%D0%BE?view=icons': '/disk/Фото',
                 'https://disk.yandex.ru/client/photo': '/photounlim', PHOTO: PHOTO,
                 '/photounlim/': '/photounlim'}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_remote(value), expected)

    def test_query_decoded_once(self):
        self.assertEqual(normalize_remote('https://disk.yandex.ru/client/disk?idDialog=%2Fdisk%2F100%2525.JPG'),
                         '/disk/100%25.JPG')

    def test_reject_invalid_paths(self):
        for value in ('/disk/../secret', '/photounlim/a//b',
                      'https://other.example/client/photo?idDialog=%2Fdisk',
                      'https://disk.yandex.ru/client/photo?idDialog=',
                      'https://disk.yandex.ru/client/photo?idDialog=%2Fdisk&idDialog=%2Fphotounlim',
                      'https://disk.yandex.ru/client/photo?idDialog=%2Fphotounlim%2F..%2Fsecret'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_remote(value)


class Response(io.BytesIO):
    def __init__(self, data, mime='image/jpeg'):
        super().__init__(data)
        self.headers = Message()
        self.headers['Content-Type'] = mime
        self.headers['Content-Length'] = str(len(data))


class FakeDisk:
    def __init__(self, fail_original=False):
        self.fail_original = fail_original
        self.downloads = []
        self.file = {'path': PHOTO, 'name': '2026-10-10 19-28-15.JPG', 'type': 'file',
                     'meta': {'size': 4, 'sizes': [
                         {'name': 'XXXL', 'url': 'https://downloader.disk.yandex.ru/preview'}]}}

    def get_resource(self, path):
        if path == PHOTO:
            return self.file
        return {'path': path, 'name': 'folder', 'type': 'dir'}

    def list_children(self, path):
        if path == '/disk/folder':
            return [{'path': path + '/nested', 'name': 'nested', 'type': 'dir'}]
        return [self.file]

    def download_url(self, path):
        self.downloads.append(path)
        if self.fail_original:
            raise RuntimeError('original unavailable')
        return 'original'

    def open_download(self, url):
        return Response(b'jpeg')


class DownloadTests(unittest.TestCase):
    def copy(self, client, remote, root):
        counts = Counter()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            copy_remote(client, remote, root, root, 1, 30, 10, counts)
        return counts

    def test_selected_file_and_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            client = FakeDisk()
            counts = self.copy(client, PHOTO, root)
            self.assertEqual((root / client.file['name']).read_bytes(), b'jpeg')
            self.assertEqual(counts['downloaded'], 1)
            self.assertEqual(self.copy(client, PHOTO, root)['skipped'], 1)
            self.assertEqual(client.downloads, [PHOTO])

    def test_selected_file_preview_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            counts = self.copy(FakeDisk(fail_original=True), PHOTO, root)
            self.assertEqual(counts['errors'], 1)
            self.assertEqual(counts['previewed'], 1)
            self.assertEqual((root / 'preview-2026-10-10 19-28-15.jpg').read_bytes(), b'jpeg')

    def test_folder_recursion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            client = FakeDisk()
            counts = self.copy(client, '/disk/folder', root)
            self.assertEqual(counts['directories'], 1)
            self.assertEqual((root / 'nested' / client.file['name']).read_bytes(), b'jpeg')

    def test_metadata_selects_exact_resource(self):
        client = object.__new__(DiskWeb)
        with patch.object(client, 'call', return_value={'resources': [
            {'path': '/disk/other', 'type': 'file'}, FakeDisk().file,
        ]}) as call:
            self.assertEqual(client.get_resource(PHOTO)['path'], PHOTO)
            self.assertEqual(call.call_args.args[1]['idContext'], PHOTO)
        with patch.object(client, 'call', return_value={'resources': []}), self.assertRaises(RuntimeError):
            client.get_resource(PHOTO)


if __name__ == '__main__':
    unittest.main()
