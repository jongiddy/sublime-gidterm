import codecs
from datetime import timedelta
import os
import shutil
import sys
import tempfile
import time
from unittest import TestCase

import sublime  # type: ignore
from unittesting import DeferrableTestCase

version = sublime.version()

gidterm = sys.modules["sublime-gidterm.gidterm"]


sublime.load_settings("Preferences.sublime-settings").set("close_windows_when_empty", False)


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


class TestTimeDeltaSeconds(DeferrableTestCase):

    def test_low_fraction(self):
        actual = timedelta(seconds=3, milliseconds=450)
        self.assertEqual(
            '0:00:03',
            str(gidterm.timedelta_seconds(actual.total_seconds()))
        )

    def test_high_fraction(self):
        actual = timedelta(seconds=3, milliseconds=550)
        self.assertEqual(
            '0:00:04',
            str(gidterm.timedelta_seconds(actual.total_seconds()))
        )


def send_command(view, command):
    # type: (sublime.View, str) -> None
    view.run_command('gidterm_send', {'characters': command})
    view.run_command('gidterm_capability', {'cap': 'cr'})


def wait_for_line(view, regex, timeout=5):
    # (sublime.View, str, int) -> Iterator[int]
    start = time.time()
    region = view.find(regex, 0)
    while region.begin() == -1:
        if time.time() - start > timeout:
            raise TimeoutError(view.substr(sublime.Region(0, view.size())))
        yield 100
        region = view.find(regex, 0)


def close_view(view):
    # type: (sublime.View) -> None
    view.set_scratch(True)
    window = sublime.active_window()
    window.focus_view(view)
    window.run_command("close_file")


class TestGidTermCommand(DeferrableTestCase):

    def test_gidterm_command_from_gidterm(self):
        # From an existing view, running the `gidterm` command creates a GidTerm
        window = sublime.active_window()
        view0 = window.new_file()
        try:
            view0.run_command('gidterm')
            view1 = window.active_view()
            self.assertNotEqual(view0, view1)
        finally:
            close_view(view0)
        try:
            self.assertNotEqual(view1.settings().get('current_working_directory'), '/tmp')

            yield from wait_for_line(view1, r'.')

            send_command(view1, 'cd /tmp')
            send_command(view1, 'export TEST_VAR=apple')
            send_command(view1, 'echo DONE')
            yield from wait_for_line(view1, r'^DONE$')

            self.assertEqual(view1.settings().get('current_working_directory'), '/tmp')

            # From an existing GidTerm view, running the `gidterm` command
            # creates a new GidTerm with the same pwd and environment.
            view1.run_command('gidterm')
            view2 = window.active_view()
            yield from wait_for_line(view2, r'.')
            self.assertNotEqual(view1, view2)
        finally:
            # Closing view1 before view2 causes ST to hang, even when done manually
            close_view(view1)
            pass
        try:
            self.assertEqual(view2.settings().get('current_working_directory'), '/tmp')
            send_command(view2, 'echo $TEST_VAR')
            yield from wait_for_line(view2, r'^apple$')
        finally:
            close_view(view2)
            # close_view(view1)

    def test_gidterm_command_from_file(self):
        # From an existing file view, running the `gidterm` command creates a
        # GidTerm in the same directory as the file.
        with tempfile.NamedTemporaryFile() as f:
            window = sublime.active_window()
            view0 = window.open_file(f.name)
            try:
                view0.run_command('gidterm')
                view1 = window.active_view()
                self.assertNotEqual(view0, view1)
            finally:
                close_view(view0)
            try:
                self.assertEqual(view1.settings().get('current_working_directory'), os.path.dirname(f.name))
            finally:
                close_view(view1)
