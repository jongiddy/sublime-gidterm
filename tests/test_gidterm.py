import codecs
from datetime import timedelta
import os
import re
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
            str(gidterm.timedelta_seconds(actual.total_seconds()))
        )

    def test_high_fraction(self):
        actual = timedelta(seconds=3, milliseconds=550)
        self.assertEqual(
            '0:00:04',
            str(gidterm.timedelta_seconds(actual.total_seconds()))
        )


class GidTermTestHelper:

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

    def assertBrowseMode(self):
        self.assertFalse(self.view.settings().get('gidterm_follow'))

    def assertTerminalMode(self):
        self.assertTrue(self.view.settings().get('gidterm_follow'))


class TestGidTermOnce(TestCase, GidTermTestHelper):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.view = gidterm.create_view(sublime.active_window(), self.tmpdir)
        self.gview = gidterm.ShellTab(self.view.id())
        self.gview.start(None)
        # make sure we have a window to work with
        s = sublime.load_settings("Preferences.sublime-settings")
        s.set("close_windows_when_empty", False)

    def tearDown(self):
        if self.gview:
            self.gview.disconnect()
        if self.view:
            self.view.set_scratch(True)
            self.view.window().focus_view(self.view)
            self.view.window().run_command("close_file")
        shutil.rmtree(self.tmpdir)

    def wait_for_prompt(self, timeout=5):
        start = time.time()
        self.gview.once()
        while not self.gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)
            self.gview.once()

    def wait_for_idle(self, timeout=5):
        start = time.time()
        while self.gview.once():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)

    def send_command(self, command, timeout=5):
        # Send command to terminal. Once this returns, we can be sure that
        # we're not at the initial prompt, so it is safe to call
        # `wait_for_prompt` immediately to wait for the command to complete.
        # We also ensure that the entire command (except the trailing newline)
        # has been echoed and received.
        start = time.time()
        self.wait_for_prompt(timeout)
        c0 = self.single_cursor()
        self.view.run_command('gidterm_send', {'characters': command})
        self.gview.once()
        while self.gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)
            self.gview.once()
        self.wait_for_idle(start + timeout - time.time())
        c1 = self.single_cursor()
        # any CR in input is replaced by newline and PS2 prompt
        expected = command.replace('\r', '\n> ')
        self.assertEqual(expected, self.view.substr(sublime.Region(c0, c1)))
        self.view.run_command('gidterm_send_cap', {'cap': 'cr'})

    def test_at_prompt(self):
        self.wait_for_prompt()
        self.assertTrue(self.gview.at_prompt())
        self.send_command('sleep 1')
        time.sleep(0.2)
        self.gview.once()
        self.assertFalse(self.gview.at_prompt())

    def test_initial_state(self):
        self.wait_for_prompt()
        self.assertTerminalMode()

    def assert_exit_code(self, code, output):
        # In the output there is a line that starts with the status code
        # and includes the Enter symbol in it.
        match = re.search(
            r'^{}[^0-9\n]??.*?\u23ce '.format(code), output, re.MULTILINE
        )
        self.assertTrue(match, output)
        # $? contains the same code
        self.assertTrue(self.gview.at_prompt())
        command = 'echo $?'
        self.send_command(command)
        c1 = self.single_cursor()
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\n{}\n'.format(code)),
            repr(self.all_contents())
        )

    def test_echo(self):
        self.wait_for_prompt()
        self.assertTerminalMode()
        command = 'echo Hello World!'
        self.send_command(command)
        c1 = self.single_cursor()
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\nHello World!\n'),
            repr(self.all_contents())
        )
        self.assert_exit_code('0', output)

    def test_fail(self):
        self.wait_for_prompt()
        self.assertTerminalMode()
        command = "sh -c 'exit 4'"
        self.send_command(command)
        c1 = self.single_cursor()
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assert_exit_code('4', output)

    def test_signal(self):
        self.wait_for_prompt()
        self.assertTerminalMode()
        command = 'sleep 30'
        self.send_command(command)
        c1 = self.single_cursor()
        time.sleep(0.5)
        self.view.run_command('gidterm_send', {'characters': '\x03'})
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assert_exit_code('130', output)

    def test_extra_whitespace(self):
        self.wait_for_prompt()
        self.assertTerminalMode()
        command = '  echo  Hello  World!  '
        self.send_command(command)
        c1 = self.single_cursor()
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


class TestGidTermLoop(TestCase, GidTermTestHelper):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.view = gidterm.create_view(sublime.active_window(), self.tmpdir)
        self.gview = gidterm.ShellTab(self.view.id())
        self.gview.start(50)
        # make sure we have a window to work with
        s = sublime.load_settings("Preferences.sublime-settings")
        s.set("close_windows_when_empty", False)

    def tearDown(self):
        if self.gview:
            self.gview.disconnect()
        if self.view:
            self.view.set_scratch(True)
            self.view.window().focus_view(self.view)
            self.view.window().run_command("close_file")
        shutil.rmtree(self.tmpdir)

    def wait_for_prompt(self, timeout=5):
        start = time.time()
        while not self.gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)

    def send_command(self, command, timeout=5):
        # Send command to terminal. Once this returns, we can be sure that
        # we're not at the initial prompt, so it is safe to call
        # `wait_for_prompt` immediately to wait for the command to complete.
        start = time.time()
        self.wait_for_prompt(timeout)
        self.view.run_command('gidterm_send', {'characters': command})
        while self.gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)
        self.view.run_command('gidterm_send_cap', {'cap': 'cr'})

    # There are two types of disconnection:
    # - the Shell instance exits (e.g. user enters Ctrl-D)
    # - the module gets reloaded, removing the GidTerm instance (e.g. move to
    # another project and back, or modify `gidterm.py`)

    def disconnect_reconnect(self):
        self.view.run_command('gidterm_send', {'characters': '\x04'})  # Ctrl-D
        time.sleep(0.2)
        self.assertBrowseMode()
        self.view.run_command('gidterm_send', {'characters': 'e'})
        time.sleep(0.2)
        self.assertBrowseMode()
        self.view.run_command('gidterm_send_cap', {'cap': 'cr'})
        self.assertIs(gidterm._viewmap[self.view.id()], self.gview)
        self.wait_for_prompt()

    def disconnect_reload(self):
        # Using `sublime_plugin.reload_plugin('sublime-gidterm')` doesn't seem
        # to work, so empty the module global `_viewmap` instead.
        gidterm._viewmap = {}
        self.view.run_command('gidterm_send', {'characters': 'e'})
        time.sleep(0.2)
        self.assertBrowseMode()
        self.view.run_command('gidterm_send_cap', {'cap': 'cr'})
        self.assertIsNot(gidterm._viewmap[self.view.id()], self.gview)
        self.gview = gidterm._viewmap[self.view.id()]
        self.wait_for_prompt()

    def test_disconnect(self):
        """
        Disconnection puts editor in browse mode. Performing a terminal
        action starts a new shell.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        self.disconnect_reconnect()
        self.assertTerminalMode()
        command = 'echo hello'
        c1 = self.single_cursor()
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            'echo hello\nhello\n' in output,
            repr((output, self.all_contents(), c1))
        )
        self.assertTerminalMode()
        self.assertEqual(self.tmpdir, self.gview.pwd)

    def test_reload(self):
        """
        Disconnection puts editor in browse mode. Performing a terminal
        action starts a new shell.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        self.disconnect_reload()
        self.assertTerminalMode()
        command = 'echo hello'
        c1 = self.single_cursor()
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            'echo hello\nhello\n' in output,
            repr((output, self.all_contents(), c1))
        )
        self.assertTerminalMode()
        self.assertEqual(self.tmpdir, self.gview.pwd)

    def test_reload_history(self):
        """
        Reconnection can access history before disconnection.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        command = 'echo hello'
        c1 = self.single_cursor()
        self.send_command(command)
        self.wait_for_prompt()
        self.disconnect_reload()
        self.assertTerminalMode()
        self.view.run_command('gidterm_escape', {'key': 'shift+ctrl+pageup'})
        region = self.single_selection()
        self.assertEqual(c1, region.begin())
        self.assertEqual(command + '\n', self.view.substr(region))

    def test_reload_environment(self):
        """
        Reconnection can access environment before disconnection.
        """
        self.wait_for_prompt()
        self.assertTerminalMode()
        command = 'export TEST_VAR=5'
        self.send_command(command)
        self.wait_for_prompt()
        self.disconnect_reload()
        self.assertTerminalMode()
        command = 'echo $TEST_VAR'
        c1 = self.single_cursor()
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor()
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertIn('\n5\n', output, output)


class TestGidTermContext(TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.view = gidterm.create_view(sublime.active_window(), self.tmpdir)
        self.gview = gidterm.ShellTab(self.view.id())
        # make sure we have a window to work with
        s = sublime.load_settings("Preferences.sublime-settings")
        s.set("close_windows_when_empty", False)

    def tearDown(self):
        if self.gview:
            self.gview.disconnect()
        if self.view:
            self.view.set_scratch(True)
            self.view.window().focus_view(self.view)
            self.view.window().run_command("close_file")
        shutil.rmtree(self.tmpdir)

    def test_description_directory(self):
        gc = gidterm.gidterm_context(self.view)

        # Empty area
        self.view.run_command('append', {'characters': '   \n', 'force': True})
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message, 'GidTerm: ' + gidterm.ACTION_LIST_CURRENT_DIRECTORY
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_DIR_LIST)
        self.assertEqual(path, self.tmpdir)

        # Dot
        self.view.run_command('append', {'characters': '.\n', 'force': True})
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message, 'GidTerm: ' + gidterm.ACTION_LIST_CURRENT_DIRECTORY
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_DIR_LIST)
        self.assertEqual(path, self.tmpdir)

        # Double-dot
        self.view.run_command('append', {'characters': '..\n', 'force': True})
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message, 'GidTerm: ' + gidterm.ACTION_GOTO_PARENT_DIRECTORY
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_DIR_CHANGE)
        self.assertEqual(path, os.path.dirname(self.tmpdir))

        subdir = os.path.join(self.tmpdir, 'subdir')
        os.mkdir(subdir)

        # Absolute path of sub-directory
        self.view.run_command(
            'append', {'characters': subdir + '\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_DIR_CHANGE + ' subdir'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_DIR_CHANGE)
        self.assertEqual(path, subdir)

        self.view.run_command(
            'append', {'characters': 'subdir\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_DIR_CHANGE + ' subdir'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_DIR_CHANGE)
        self.assertEqual(path, subdir)

    def test_description_file(self):
        gc = gidterm.gidterm_context(self.view)

        file_absent = os.path.join(self.tmpdir, 'absent')
        file_present = os.path.join(self.tmpdir, 'present')
        with open(file_present, 'w'):
            pass
        also_present = os.path.join(self.tmpdir, 'also present')
        with open(also_present, 'w'):
            pass

        # Absolute path of non-existing file
        self.view.run_command(
            'append', {'characters': file_absent + '\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message, 'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_NEW + ' absent'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_NEW)
        self.assertEqual(path, file_absent)

        # Relative path of non-existing file
        self.view.run_command(
            'append', {'characters': 'absent\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message, 'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_NEW + ' absent'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_NEW)
        self.assertEqual(path, file_absent)

        # Absolute path of existing file
        self.view.run_command(
            'append', {'characters': file_present + '\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_OPEN + ' present'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_OPEN)
        self.assertEqual(path, file_present)

        # Relative path of existing file
        self.view.run_command(
            'append', {'characters': 'present\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_OPEN + ' present'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_OPEN)
        self.assertEqual(path, file_present)

        # Clicking after space can find name with space
        self.view.run_command(
            'append', {'characters': also_present + '\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_OPEN + " 'also present'"
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_OPEN)
        self.assertEqual(path, also_present)

        # Clicking before space can find name with space
        self.view.run_command(
            'append', {'characters': also_present + '\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 12)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_OPEN + " 'also present'"
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_OPEN)
        self.assertEqual(path, also_present)

        # Clicking on point that matches two names returns the longer name
        self.view.run_command(
            'append', {'characters': 'also present\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 2)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_OPEN + " 'also present'"
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_OPEN)
        self.assertEqual(path, also_present)

        # Absolute path of existing file with row
        self.view.run_command(
            'append', {'characters': file_present + ':12\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 5)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_GOTO + ' present:12'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_GOTO)
        self.assertEqual(path, file_present + ':12:0')

        # Relative path of existing file with row
        self.view.run_command(
            'append', {'characters': 'present:12\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 5)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_GOTO + ' present:12'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_GOTO)
        self.assertEqual(path, file_present + ':12:0')

        # Absolute path of existing file with row and column
        self.view.run_command(
            'append', {'characters': file_present + ':12:34\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 8)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_GOTO + ' present:12:34'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_GOTO)
        self.assertEqual(path, file_present + ':12:34')

        # Relative path of existing file with row and column
        self.view.run_command(
            'append', {'characters': 'present:12:34\n', 'force': True}
        )
        x, y = self.view.text_to_window(self.view.size() - 8)
        event = {'x': x, 'y': y}
        message = gc.description(event)
        action, path = self.view.settings().get('gidterm_context')
        self.assertEqual(
            message,
            'GidTerm: ' + gidterm.CONTEXT_ACTION_FILE_GOTO + ' present:12:34'
        )
        self.assertEqual(action, gidterm.CONTEXT_ACTION_FILE_GOTO)
        self.assertEqual(path, file_present + ':12:34')

