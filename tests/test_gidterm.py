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

    def single_selection(self):
        sel = self.view.sel()
        regions = list(sel)
        self.assertEqual(1, len(regions))
        return regions[0]

    def single_cursor(self):
        region = self.single_selection()
        self.assertTrue(region.empty())
        return region.begin()

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

    def send_command(self, command):
        self.assertTrue(self.shell.at_prompt())
        self.shell.send(command)
        # give the shell time to echo command
        time.sleep(0.1)
        self.wait_for_idle()
        self.shell.send(gidterm._terminal_capability_map['cr'])

    def assertBrowseMode(self):
        self.assertFalse(self.view.settings().get('gidterm_follow'))

    def assertTerminalMode(self):
        self.assertTrue(self.view.settings().get('gidterm_follow'))

    def test_at_prompt(self):
        self.wait_for_prompt()
        self.assertTrue(self.shell.at_prompt())
        end = self.view.size()
        last = self.view.substr(sublime.Region(end - 3, end))
        self.assertEqual('\n$ ', last, repr(last))
        self.send_command('sleep 1')
        time.sleep(0.2)
        self.shell.once()
        self.assertFalse(self.shell.at_prompt())

    def test_initial_state(self):
        self.wait_for_prompt()
        self.assertTerminalMode()

    def test_echo(self):
        self.wait_for_prompt()
        self.assertTerminalMode()
        c0 = self.single_cursor()
        command = 'echo Hello World!'
        self.send_command(command)
        c1 = self.single_cursor()
        self.assertEqual(command, self.view.substr(sublime.Region(c0, c1)))
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\nHello World!\n'),
            repr(self.all_contents())
        )
        self.assertTerminalMode()

    def test_extra_whitespace(self):
        self.wait_for_prompt()
        self.assertTerminalMode()
        c0 = self.single_cursor()
        command = '  echo  Hello  World!  '
        self.send_command(command)
        c1 = self.single_cursor()
        self.assertEqual(command, self.view.substr(sublime.Region(c0, c1)))
        self.wait_for_prompt()
        c1 -= 2  # GidTerm will remove the trailing spaces
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\nHello World!\n'),
            (output, self.all_contents(), c1)
        )
        self.assertTerminalMode()

    def test_terminal_mode_home(self):
        """
        In terminal mode the Home key:
        - changes to browse mode; and
        - moves to start of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        row0, col0 = self.view.rowcol(self.single_cursor())
        self.view.run_command("gidterm_escape", {"key": "home"})
        self.assertBrowseMode()
        row1, col1 = self.view.rowcol(self.single_cursor())
        self.assertEqual(row0, row1)
        self.assertEqual(0, col1)

    def test_terminal_mode_end(self):
        """
        In terminal mode the End key:
        - changes to browse mode; and
        - moves to end of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        row0, col0 = self.view.rowcol(self.single_cursor())
        self.view.run_command("gidterm_escape", {"key": "end"})
        self.assertBrowseMode()
        cursor1 = self.single_cursor()
        row1, col1 = self.view.rowcol(cursor1)
        self.assertEqual(row0, row1)
        self.assertNotEqual(
            0, self.view.classify(cursor1) & sublime.CLASS_LINE_END
        )

    def test_terminal_mode_ctrl_home(self):
        """
        In terminal mode the Ctrl-Home key:
        - changes to browse mode; and
        - moves to start of file.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        self.view.run_command("gidterm_escape", {"key": "ctrl+home"})
        self.assertBrowseMode()
        cursor = self.single_cursor()
        self.assertEqual(0, cursor)

    def test_terminal_mode_ctrl_end(self):
        """
        In terminal mode the Ctrl-End key:
        - changes to browse mode; and
        - moves to end of file.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        self.view.run_command("gidterm_escape", {"key": "ctrl+end"})
        self.assertBrowseMode()
        cursor = self.single_cursor()
        self.assertEqual(self.view.size(), cursor)

    def test_terminal_mode_shift_home(self):
        """
        In terminal mode the Shift-Home key:
        - changes to browse mode; and
        - selects to start of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+home"})
        self.assertBrowseMode()
        region = self.single_selection()
        row1, col1 = self.view.rowcol(region.begin())
        self.assertEqual(cursor0, region.end())
        self.assertEqual(row0, row1)
        self.assertEqual(0, col1)

    def test_terminal_mode_shift_end(self):
        """
        In terminal mode the Shift-End key:
        - changes to browse mode; and
        - selects to end of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+end"})
        # Puts shell in browse mode
        self.assertBrowseMode()
        # Moves to end of line
        region = self.single_selection()
        row1, col1 = self.view.rowcol(region.end())
        self.assertEqual(cursor0, region.begin())
        self.assertNotEqual(
            0, self.view.classify(region.end()) & sublime.CLASS_LINE_END
        )
        self.assertEqual(row0, row1)

    def test_terminal_mode_shift_ctrl_home(self):
        """
        In terminal mode the Shift-Ctrl-Home key:
        - changes to browse mode; and
        - selects to start of file.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+home"})
        self.assertBrowseMode()
        region = self.single_selection()
        row1, col1 = self.view.rowcol(region.begin())
        self.assertEqual(0, region.begin())
        self.assertEqual(cursor0, region.end())

    def test_terminal_mode_shift_ctrl_end(self):
        """
        In terminal mode the Shift-Ctrl-End key:
        - changes to browse mode; and
        - selects to end of file.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+end"})
        self.assertBrowseMode()
        region = self.single_selection()
        row1, col1 = self.view.rowcol(region.end())
        self.assertEqual(cursor0, region.begin())
        self.assertEqual(self.view.size(), region.end())

    def test_terminal_mode_page_up(self):
        """
        In terminal mode the PageUp key:
        - changes to browse mode; and
        - moves the cursor at or somewhere before its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        self.view.run_command("gidterm_escape", {"key": "pageup"})
        self.assertBrowseMode()
        cursor1 = self.single_cursor()
        self.assertGreaterEqual(cursor0, cursor1)

    def test_terminal_mode_page_down(self):
        """
        In terminal mode the PageDown key:
        - changes to browse mode; and
        - moves the cursor at or somewhere after its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        self.view.run_command("gidterm_escape", {"key": "pagedown"})
        self.assertBrowseMode()
        cursor1 = self.single_cursor()
        self.assertLessEqual(cursor0, cursor1)

    # Ctrl-PageUp/Down typically moves to a new Sublime Edit tab

    def test_terminal_mode_shift_page_up(self):
        """
        In terminal mode the Shift-PageUp key:
        - changes to browse mode; and
        - selects from the cursor to somewhere before its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        self.view.run_command("gidterm_escape", {"key": "shift+pageup"})
        self.assertBrowseMode()
        region = self.single_selection()
        self.assertGreaterEqual(cursor0, region.begin())
        self.assertEqual(cursor0, region.end())

    def test_terminal_mode_shift_page_down(self):
        """
        In terminal mode the Shift-PageDown key:
        - changes to browse mode; and
        - selects from the cursor to somewhere after its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cursor0 = self.single_cursor()
        self.view.run_command("gidterm_escape", {"key": "shift+pagedown"})
        self.assertBrowseMode()
        region = self.single_selection()
        self.assertEqual(cursor0, region.begin())
        self.assertLessEqual(cursor0, region.end())

    def test_terminal_mode_shift_ctrl_page(self):
        """
        The Ctrl-Shift-PageUp/Down key:
        - changes to browse mode; and
        - selects the previous/next command.
        When running off the end of the history, when Ctrl-Shift-PageDown:
        - changes to terminal mode; and
        - places the cursor in the original position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        cmd0_begin = self.single_cursor()
        self.send_command("echo test")
        cmd0_end = self.single_cursor() + 1
        self.wait_for_prompt()
        cmd1_begin = self.single_cursor()
        self.send_command("echo '1\r2\r3'")
        cmd1_end = self.single_cursor() + 1
        self.wait_for_prompt()
        cmd2_prompt = self.single_cursor()
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pageup"})
        self.assertBrowseMode()
        regions = list(self.view.sel())
        self.assertEqual(3, len(regions))
        self.assertEqual(cmd1_begin, regions[0].begin())
        self.assertEqual(cmd1_end, regions[-1].end())
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pageup"})
        region = self.single_selection()
        self.assertEqual(cmd0_begin, region.begin())
        # add one because we select the final newline as well
        self.assertEqual(cmd0_end, region.end())
        # At first command, Ctrl-Shift-PageUp stays on first command
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pageup"})
        region = self.single_selection()
        self.assertEqual(cmd0_begin, region.begin())
        self.assertEqual(cmd0_end, region.end())
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pagedown"})
        self.assertBrowseMode()
        regions = list(self.view.sel())
        self.assertEqual(3, len(regions))
        self.assertEqual(cmd1_begin, regions[0].begin())
        self.assertEqual(cmd1_end, regions[-1].end())
        # moving back to latest command changes to terminal mode again
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pagedown"})
        self.assertTerminalMode()
        self.assertEqual(cmd2_prompt, self.single_cursor())
        # doing it again stays in the same state
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pagedown"})
        self.assertTerminalMode()
        self.assertEqual(cmd2_prompt, self.single_cursor())
