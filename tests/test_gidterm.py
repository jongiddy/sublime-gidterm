import codecs
from datetime import timedelta
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

    def all_contents(self, view):
        return view.substr(sublime.Region(0, view.size()))

    def single_selection(self, view):
        sel = view.sel()
        regions = list(sel)
        self.assertEqual(1, len(regions))
        return regions[0]

    def single_cursor(self, view):
        region = self.single_selection(view)
        self.assertTrue(region.empty())
        return region.begin()

    def assertBrowseMode(self, view):
        self.assertFalse(view.settings().get('gidterm_follow'))

    def assertTerminalMode(self, view):
        self.assertTrue(view.settings().get('gidterm_follow'))


class TestGidTermCommand(TestCase, GidTermTestHelper):

    @staticmethod
    def wait_for_prompt(gview, timeout=5):
        start = time.time()
        while not gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)

    def send_command(self, gview, command, timeout=5):
        # Send command to terminal. Once this returns, we can be sure that
        # we're not at the initial prompt, so it is safe to call
        # `wait_for_prompt` immediately to wait for the command to complete.
        start = time.time()
        self.wait_for_prompt(gview, timeout)
        gview.run_command('gidterm_send', {'characters': command})
        while gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)
        gview.run_command('gidterm_send_cap', {'cap': 'cr'})

    def test_gidterm_command(self):
        window = sublime.active_window()
        view0 = window.new_file()
        try:
            view0.run_command('gidterm')
            view1 = window.active_view()
            self.assertNotEqual(view0, view1)
        finally:
            view0.set_scratch(True)
            view0.window().focus_view(view0)
            view0.window().run_command("close_file")
        try:
            gview1 = gidterm._viewmap[view1.id()]
            self.wait_for_prompt(gview1)
            self.send_command(gview1, 'cd /tmp')
            self.wait_for_prompt(gview1)
            self.send_command(gview1, 'export TEST_VAR=apple')
            self.wait_for_prompt(gview1)

            # Start another GidTerm from this one, and check
            # that it has the same pwd and environment
            gview1.run_command('gidterm')
            view2 = window.active_view()
            self.assertNotEqual(view1, view2)
        finally:
            view1.set_scratch(True)
            view1.window().focus_view(view1)
            view1.window().run_command("close_file")
        try:
            gview2 = gidterm._viewmap[view2.id()]
            self.wait_for_prompt(gview2)
            self.assertEqual(gview2.pwd, '/tmp')
            command = 'echo $TEST_VAR'
            c1 = self.single_cursor(view2)
            self.send_command(gview2, command)
            self.wait_for_prompt(gview2)
            c2 = self.single_cursor(view2)
            output = view2.substr(sublime.Region(c1, c2))
            self.assertIn('\napple\n', output, output)
        finally:
            view2.set_scratch(True)
            view2.window().focus_view(view2)
            view2.window().run_command("close_file")


class TestGidTermOnce(TestCase, GidTermTestHelper):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.view = gidterm.create_view(
            sublime.active_window(), self.tmpdir, gidterm._initial_profile
        )
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
        c0 = self.single_cursor(self.view)
        self.view.run_command('gidterm_send', {'characters': command})
        self.gview.once()
        while self.gview.at_prompt():
            if time.time() - start > timeout:
                raise TimeoutError()
            time.sleep(0.1)
            self.gview.once()
        self.wait_for_idle(start + timeout - time.time())
        c1 = self.single_cursor(self.view)
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
        self.assertTerminalMode(self.view)

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
        c1 = self.single_cursor(self.view)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\n{}\n'.format(code)),
            repr(self.all_contents(self.view))
        )

    def test_echo(self):
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = 'echo Hello World!'
        self.send_command(command)
        c1 = self.single_cursor(self.view)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\nHello World!\n'),
            repr(self.all_contents(self.view))
        )
        self.assert_exit_code('0', output)

    def test_fail(self):
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = "sh -c 'exit 4'"
        self.send_command(command)
        c1 = self.single_cursor(self.view)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assert_exit_code('4', output)

    def test_signal(self):
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = 'sleep 30'
        self.send_command(command)
        c1 = self.single_cursor(self.view)
        time.sleep(0.5)
        self.view.run_command('gidterm_send', {'characters': '\x03'})
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assert_exit_code('130', output)

    def test_extra_whitespace(self):
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = '  echo  Hello  World!  '
        self.send_command(command)
        c1 = self.single_cursor(self.view)
        self.wait_for_prompt()
        c1 -= 2  # GidTerm will remove the trailing spaces
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            output.startswith('\nHello World!\n'),
            (output, self.all_contents(self.view), c1)
        )
        self.assertTerminalMode(self.view)

    def test_terminal_mode_home(self):
        """
        In terminal mode the Home key:
        - changes to browse mode; and
        - moves to start of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        row0, col0 = self.view.rowcol(self.single_cursor(self.view))
        self.view.run_command("gidterm_escape", {"key": "home"})
        self.assertBrowseMode(self.view)
        row1, col1 = self.view.rowcol(self.single_cursor(self.view))
        self.assertEqual(row0, row1)
        self.assertEqual(0, col1)

    def test_terminal_mode_end(self):
        """
        In terminal mode the End key:
        - changes to browse mode; and
        - moves to end of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        row0, col0 = self.view.rowcol(self.single_cursor(self.view))
        self.view.run_command("gidterm_escape", {"key": "end"})
        self.assertBrowseMode(self.view)
        cursor1 = self.single_cursor(self.view)
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
        self.assertTerminalMode(self.view)
        self.view.run_command("gidterm_escape", {"key": "ctrl+home"})
        self.assertBrowseMode(self.view)
        cursor = self.single_cursor(self.view)
        self.assertEqual(0, cursor)

    def test_terminal_mode_ctrl_end(self):
        """
        In terminal mode the Ctrl-End key:
        - changes to browse mode; and
        - moves to end of file.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        self.view.run_command("gidterm_escape", {"key": "ctrl+end"})
        self.assertBrowseMode(self.view)
        cursor = self.single_cursor(self.view)
        self.assertEqual(self.view.size(), cursor)

    def test_terminal_mode_shift_home(self):
        """
        In terminal mode the Shift-Home key:
        - changes to browse mode; and
        - selects to start of line.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+home"})
        self.assertBrowseMode(self.view)
        region = self.single_selection(self.view)
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
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+end"})
        # Puts shell in browse mode
        self.assertBrowseMode(self.view)
        # Moves to end of line
        region = self.single_selection(self.view)
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
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+home"})
        self.assertBrowseMode(self.view)
        region = self.single_selection(self.view)
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
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        row0, col0 = self.view.rowcol(cursor0)
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+end"})
        self.assertBrowseMode(self.view)
        region = self.single_selection(self.view)
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
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        self.view.run_command("gidterm_escape", {"key": "pageup"})
        self.assertBrowseMode(self.view)
        cursor1 = self.single_cursor(self.view)
        self.assertGreaterEqual(cursor0, cursor1)

    def test_terminal_mode_page_down(self):
        """
        In terminal mode the PageDown key:
        - changes to browse mode; and
        - moves the cursor at or somewhere after its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        self.view.run_command("gidterm_escape", {"key": "pagedown"})
        self.assertBrowseMode(self.view)
        cursor1 = self.single_cursor(self.view)
        self.assertLessEqual(cursor0, cursor1)

    # Ctrl-PageUp/Down typically moves to a new Sublime Edit tab

    def test_terminal_mode_shift_page_up(self):
        """
        In terminal mode the Shift-PageUp key:
        - changes to browse mode; and
        - selects from the cursor to somewhere before its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        self.view.run_command("gidterm_escape", {"key": "shift+pageup"})
        self.assertBrowseMode(self.view)
        region = self.single_selection(self.view)
        self.assertGreaterEqual(cursor0, region.begin())
        self.assertEqual(cursor0, region.end())

    def test_terminal_mode_shift_page_down(self):
        """
        In terminal mode the Shift-PageDown key:
        - changes to browse mode; and
        - selects from the cursor to somewhere after its current position.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        cursor0 = self.single_cursor(self.view)
        self.view.run_command("gidterm_escape", {"key": "shift+pagedown"})
        self.assertBrowseMode(self.view)
        region = self.single_selection(self.view)
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
        self.assertTerminalMode(self.view)
        cmd0_begin = self.single_cursor(self.view)
        self.send_command("echo test")
        cmd0_end = self.single_cursor(self.view) + 1
        self.wait_for_prompt()
        cmd1_begin = self.single_cursor(self.view)
        self.send_command("echo '1\r2\r3'")
        cmd1_end = self.single_cursor(self.view) + 1
        self.wait_for_prompt()
        cmd2_prompt = self.single_cursor(self.view)
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pageup"})
        self.assertBrowseMode(self.view)
        regions = list(self.view.sel())
        self.assertEqual(3, len(regions))
        self.assertEqual(cmd1_begin, regions[0].begin())
        self.assertEqual(cmd1_end, regions[-1].end())
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pageup"})
        region = self.single_selection(self.view)
        self.assertEqual(cmd0_begin, region.begin())
        self.assertEqual(cmd0_end, region.end())
        # At first command, Ctrl-Shift-PageUp stays on first command
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pageup"})
        region = self.single_selection(self.view)
        self.assertEqual(cmd0_begin, region.begin())
        self.assertEqual(cmd0_end, region.end())
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pagedown"})
        self.assertBrowseMode(self.view)
        regions = list(self.view.sel())
        self.assertEqual(3, len(regions))
        self.assertEqual(cmd1_begin, regions[0].begin())
        self.assertEqual(cmd1_end, regions[-1].end())
        # moving back to latest command changes to terminal mode again
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pagedown"})
        self.assertTerminalMode(self.view)
        self.assertEqual(cmd2_prompt, self.single_cursor(self.view))
        # doing it again stays in the same state
        self.view.run_command("gidterm_escape", {"key": "shift+ctrl+pagedown"})
        self.assertTerminalMode(self.view)
        self.assertEqual(cmd2_prompt, self.single_cursor(self.view))


class TestGidTermLoop(TestCase, GidTermTestHelper):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.view = gidterm.create_view(
            sublime.active_window(), self.tmpdir, gidterm._initial_profile
        )
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
        self.assertBrowseMode(self.view)
        self.view.run_command('gidterm_send_cap', {'cap': 'cr'})
        self.assertIs(gidterm._viewmap[self.view.id()], self.gview)
        self.wait_for_prompt()

    def disconnect_reload(self):
        # When switching projects, each view gets its `on_close` handler run.
        for view in list(gidterm._viewmap.values()):
            listener = gidterm.GidtermListener(self.view)
            listener.on_close()
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
        self.assertTerminalMode(self.view)
        self.disconnect_reconnect()
        self.assertTerminalMode(self.view)
        command = 'echo hello'
        c1 = self.single_cursor(self.view)
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            'echo hello\nhello\n' in output,
            repr((output, self.all_contents(self.view), c1))
        )
        self.assertTerminalMode(self.view)
        self.assertEqual(self.tmpdir, self.gview.pwd)

    def test_reload(self):
        """
        Disconnection puts editor in browse mode. Performing a terminal
        action starts a new shell.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        self.disconnect_reload()
        self.assertTerminalMode(self.view)
        command = 'echo hello'
        c1 = self.single_cursor(self.view)
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertTrue(
            'echo hello\nhello\n' in output,
            repr((output, self.all_contents(self.view), c1))
        )
        self.assertTerminalMode(self.view)
        self.assertEqual(self.tmpdir, self.gview.pwd)

    def test_reload_history(self):
        """
        Reconnection can access history before disconnection.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = 'echo hello'
        c1 = self.single_cursor(self.view)
        self.send_command(command)
        self.wait_for_prompt()
        self.disconnect_reload()
        self.assertTerminalMode(self.view)
        self.view.run_command('gidterm_escape', {'key': 'shift+ctrl+pageup'})
        region = self.single_selection(self.view)
        self.assertEqual(c1, region.begin())
        self.assertEqual(command + '\n', self.view.substr(region))

    def test_reconnect_environment(self):
        """
        Reconnection can access environment before disconnection.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = 'export TEST_VAR=5'
        self.send_command(command)
        self.wait_for_prompt()
        init_file = self.view.settings().get('gidterm_init_file')
        with open(init_file) as f:
            init_script = f.read()
        self.assertIn('TEST_VAR="5"', init_script, init_script)
        self.disconnect_reconnect()
        init_file = self.view.settings().get('gidterm_init_file')
        with open(init_file) as f:
            init_script = f.read()
        self.assertIn('TEST_VAR="5"', init_script, init_script)
        self.assertTerminalMode(self.view)
        command = 'echo $TEST_VAR'
        c1 = self.single_cursor(self.view)
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertIn('\n5\n', output, output)

    def test_reload_environment(self):
        """
        Reconnection can access environment before disconnection.
        """
        self.wait_for_prompt()
        self.assertTerminalMode(self.view)
        command = 'export TEST_VAR=5'
        self.send_command(command)
        self.wait_for_prompt()
        init_file = self.view.settings().get('gidterm_init_file')
        with open(init_file) as f:
            init_script = f.read()
        self.assertIn('TEST_VAR="5"', init_script, init_script)
        self.disconnect_reload()
        init_script = self.view.settings().get('gidterm_init')
        self.assertIn('TEST_VAR="5"', init_script, init_script)
        init_file = self.view.settings().get('gidterm_init_file')
        with open(init_file) as f:
            init_script = f.read()
        self.assertIn('TEST_VAR="5"', init_script, init_script)
        self.assertTerminalMode(self.view)
        command = 'echo $TEST_VAR'
        c1 = self.single_cursor(self.view)
        self.send_command(command)
        self.wait_for_prompt()
        c2 = self.single_cursor(self.view)
        output = self.view.substr(sublime.Region(c1, c2))
        self.assertIn('\n5\n', output, output)
