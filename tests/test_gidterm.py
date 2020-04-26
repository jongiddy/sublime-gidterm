import codecs
from datetime import timedelta
import shutil
import sys
import tempfile
import time
from unittest import TestCase

import sublime

version = sublime.version()

gidterm = sys.modules["sublime-gidterm.gidterm"]


class TimeoutError(Exception):
    pass


class TestDecode(TestCase):

    def setUp(self):
        codecs.register_error('gidterm', gidterm.gidterm_decode_error)
        utf8_decoder_factory = codecs.getincrementaldecoder('utf8')
        self.decoder = utf8_decoder_factory(errors='gidterm')

    def test_gidterm_decode_utf8(self):
        self.assertEqual(
            'gid\U0001F60Aterm',
            self.decoder.decode(b'gid\xf0\x9f\x98\x8aterm')
        )

    def test_gidterm_decode_invalid_start_byte(self):
        # not valid UTF-8 but can be decoded as ISO 8859
        self.assertEqual(
            'gid\xa0term',
            self.decoder.decode(b'gid\xa0term')
        )

    def test_gidterm_decode_invalid_continuation_byte(self):
        # not valid UTF-8 but can be decoded as ISO 8859-1
        self.assertEqual(
            'gid\u00e3\u00a0term',
            self.decoder.decode(b'gid\xe3\xa0term')
        )


class TestTimeDeltaSeconds(TestCase):

    def test_low_fraction(self):
        actual = timedelta(seconds=3, milliseconds=450)
        self.assertEqual(
            '0:00:03',
            str(gidterm.timedelta_seconds(actual))
        )

    def test_high_fraction(self):
        actual = timedelta(seconds=3, milliseconds=550)
        self.assertEqual(
            '0:00:04',
            str(gidterm.timedelta_seconds(actual))
        )


class TestGidTermLoop(TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.view = sublime.active_window().new_file()
        self.shell = gidterm.GidtermShell(self.view, self.tmpdir)
        self.shell.start(None)
        # make sure we have a window to work with
        s = sublime.load_settings("Preferences.sublime-settings")
        s.set("close_windows_when_empty", False)

    def tearDown(self):
        self.shell.close()
        if self.view:
            self.view.set_scratch(True)
            self.view.window().focus_view(self.view)
            self.view.window().run_command("close_file")
        shutil.rmtree(self.tmpdir)

    def all_contents(self):
        return self.view.substr(sublime.Region(0, self.view.size()))

    def wait_for_prompt(self, timeout=5):
        start = time.time()
        self.shell.once()
        while not self.shell.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)
            self.shell.once()

    def wait_for_idle(self, timeout=5):
        start = time.time()
        while self.shell.once():
            if time.time() - start > timeout:
                raise TimeoutError()

    def run_command(self, command):
        self.assertTrue(self.shell.at_prompt())
        cmd_start = self.view.size()
        self.shell.send(command)
        # give the shell time to echo command
        time.sleep(0.1)
        self.wait_for_idle()
        cmd_end = self.view.size()
        response = self.view.substr(sublime.Region(cmd_start, cmd_end))
        self.assertEqual(command, response, repr(response))
        self.shell.send(gidterm._terminal_capability_map['cr'])

    def test_at_prompt(self):
        self.wait_for_prompt()
        self.assertTrue(self.shell.at_prompt())
        end = self.view.size()
        last = self.view.substr(sublime.Region(end - 3, end))
        self.assertEqual('\n$ ', last, repr(last))
        self.run_command('sleep 1')
        time.sleep(0.2)
        self.shell.once()
        self.assertFalse(self.shell.at_prompt())

    def test_initial_state(self):
        self.wait_for_prompt()
        self.assertTrue(self.view.settings().get('gidterm_follow'))

    def test_echo(self):
        self.wait_for_prompt()
        self.run_command('echo Hello World!')
        start = self.view.size()
        self.wait_for_prompt()
        output = self.view.substr(sublime.Region(start, self.view.size()))
        self.assertTrue(
            output.startswith('\nHello World!\n'),
            repr(self.all_contents())
        )
