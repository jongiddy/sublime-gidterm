import codecs
from datetime import datetime, timedelta, timezone
import os
import pty
import re
import select
import signal
import tempfile

import sublime
import sublime_plugin


# Map from view id to shell
_shellmap = {}


profile = br'''
if [ -r ~/.profile ]; then . ~/.profile; fi
export PROMPT_COMMAND='PS1=\\[\\e[1p\\]\\\$@\\[$?@${VIRTUAL_ENV}@\\w\\e[~\\]'
export PROMPT_DIRTRIM=
export PS0='\e[0!p'
export PS2='\e[2!p'
export TERM=ansi
# Set LINES and COLUMNS to a standard size for commands run by the shell to
# avoid tools creating wonky output, e.g. many tools display a completion
# percentage on the right side of the screen.  man pages are formatted to fit
# the width COLUMNS.
shopt -u checkwinsize
export COLUMNS=80
export LINES=24
# Avoid paging by using cat as the default pager
export PAGER=/bin/cat
# Don't add control commands to the history
export HISTIGNORE=${HISTIGNORE:+${HISTIGNORE}:}'*# [@gidterm@]'
'''

_exit_status_info = {}

for name in dir(signal):
    if name.startswith('SIG') and not name.startswith('SIG_'):
        if name in ('SIGRTMIN', 'SIGRTMAX'):
            continue
        try:
            signum = int(getattr(signal, name))
        except Exception:
            pass
        _exit_status_info[str(signum + 128)] = '\U0001f5f2' + name


def gidterm_decode_error(e):
    # If text is not Unicode, it is most likely Latin-1. Windows-1252 is a
    # superset of Latin-1 and may be present in downloaded files.
    # TODO: Use the LANG setting to select appropriate fallback encoding
    b = e.object[e.start:e.end]
    try:
        s = b.decode('windows-1252')
    except UnicodeDecodeError:
        # If even that can't decode, fallback to using Unicode replacement char
        s = b.decode('utf8', 'replace')
    print('gidterm: [WARN] {}: replacing {!r} with {!r}'.format(
        e.reason, b, s.encode('utf8')
    ))
    return s, e.end


codecs.register_error('gidterm', gidterm_decode_error)


class Shell:

    def __init__(self):
        self.pid = None
        self.fd = None
        self.path = None
        utf8_decoder_factory = codecs.getincrementaldecoder('utf8')
        self.decoder = utf8_decoder_factory(errors='gidterm')

    def __del__(self):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.path is not None:
            os.unlink(self.path)
            self.path = None
        if self.pid is not None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                self.pid = None

    def fork(self, workdir):
        fd, self.path = tempfile.mkstemp()
        try:
            os.write(fd, profile)
        finally:
            os.close(fd)

        args = [
            'bash', '--rcfile', self.path
        ]
        env = os.environ.update({
            # If COLUMNS is the default of 80, the shell will break long
            # prompts over two lines, making them harder to search for. It also
            # allows the shell to use UP control characters to edit lines
            # during command history navigation. Setting COLUMNS to a very
            # large value avoids these behaviours.
            #
            # When displaying command completion lists, bash pages them based
            # on the LINES variable. A large LINES value avoids paging.
            'COLUMNS': '32767',
            'LINES': '32767',
            'TERM': 'ansi',
        })
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # child
            os.chdir(workdir)
            os.execvpe('bash', args, env)

    def send(self, s):
        if s:
            os.write(self.fd, s.encode('utf8'))

    def ready(self):
        fd = self.fd
        if fd is None:
            return False
        rfds, wfds, xfds = select.select([fd], [], [], 0)
        return fd in rfds

    def receive(self):
        return self.decoder.decode(os.read(self.fd, 4096))


class GidtermShell:

    _escape_pat = re.compile(
        r'(\x07|'                                       # BEL
        r'(?:\x08+)|'                                   # BACKSPACE's
        r'(?:\r+\n?)|'                                  # CR's with optional NL
        r'(?:\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]))'  # CSI
    )

    _partial_pat = re.compile(
        r'\x1b(\[[\x30-\x3f]*[\x20-\x2f]*)?$'           # CSI
    )

    def __init__(self, view, workdir):
        self.view = view
        settings = view.settings()
        view.set_name('[bash]')
        view.set_scratch(True)
        view.set_syntax_file('Packages/sublime-gidterm/gidterm.sublime-syntax')
        view.set_line_endings('Unix')
        view.set_read_only(True)
        settings = view.settings()
        settings.set('is_gidterm', True)
        settings.set('spell_check', False)
        settings.set(
            'color_scheme',
            'Packages/sublime-gidterm/gidterm.sublime-color-scheme'
        )
        settings.set('gidterm_follow', True)
        settings.set('gidterm_history', [])

        # `cursor` is the location of the input cursor. It is often the end of
        # the doc but may be earlier if the LEFT key is used, or during
        # command history rewriting.
        self.cursor = view.size()
        self.start_pos = self.cursor
        self.in_lines = None
        self.out_start_time = None
        self.prompt_type = None
        self.scope = None
        self.saved = ''

        _shellmap[view.id()] = self

        self.shell = Shell()
        self.shell.fork(workdir)
        sublime.set_timeout(self.loop, 100)

    def send(self, s):
        self.shell.send(s)

    def insert(self, start, text):
        view = self.view
        if start == view.size():
            view.run_command(
                'append',
                {'characters': text, 'force': True, 'scroll_to_end': True}
            )
            end = view.size()
        else:
            sel = view.sel()
            sel.clear()
            sel.add(start)
            view.set_read_only(False)
            try:
                view.run_command('insert', {'characters': text})
            finally:
                view.set_read_only(True)
            end = start + len(text)

        if self.scope is not None:
            regions = view.get_regions(self.scope)
            if regions and regions[-1].end() == start:
                prev = regions.pop()
                region = sublime.Region(prev.begin(), end)
            else:
                region = sublime.Region(start, end)
            regions.append(region)
            view.add_regions(
                self.scope, regions, self.scope,
                flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT
            )

        return end

    def write(self, start, text):
        view = self.view
        if start == view.size():
            view.run_command(
                'append',
                {'characters': text, 'force': True, 'scroll_to_end': True}
            )
            end = view.size()
        else:
            end = start + len(text)
            region = sublime.Region(start, end)
            sel = view.sel()
            sel.clear()
            sel.add(region)
            view.set_read_only(False)
            try:
                view.run_command('insert', {'characters': text})
            finally:
                view.set_read_only(True)

        if self.scope is not None:
            regions = view.get_regions(self.scope)
            if regions and regions[-1].end() == start:
                prev = regions.pop()
                region = sublime.Region(prev.begin(), end)
            else:
                region = sublime.Region(start, end)
            regions.append(region)
            view.add_regions(
                self.scope, regions, self.scope,
                flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT
            )

        return end

    def move_cursor(self):
        view = self.view
        follow = view.settings().get('gidterm_follow')
        if follow:
            sel = view.sel()
            sel.clear()
            sel.add(self.cursor)
            view.show(self.cursor)

    def erase(self, region):
        length = region.size()
        if length > 0:
            view = self.view
            sel = view.sel()
            sel.clear()
            sel.add(region)
            view.set_read_only(False)
            try:
                view.run_command('insert', {'characters': ' ' * length})
            finally:
                view.set_read_only(True)

    def delete(self, region):
        if not region.empty():
            view = self.view
            sel = view.sel()
            sel.clear()
            sel.add(region)
            view.set_read_only(False)
            view.run_command("left_delete")
            view.set_read_only(True)

    def unbell(self):
        self.view.set_name('[bash]')

    def at_cursor(self):
        cursor = self.cursor
        for region in self.view.sel():
            if region.begin() != cursor or region.end() != cursor:
                return False
        return True

    def handle_selection_modified(self):
        view = self.view
        sel = view.sel()
        input_first = min(self.start_pos, self.cursor)
        sel_first = input_first
        for region in sel:
            sel_first = min(sel_first, region.begin())
        if sel_first < input_first:
            # user has clicked in the area before active command:
            # change to browse mode
            view.settings().set('gidterm_follow', False)

    def handle_output(self, s, now):
        # Add any saved text from previous iteration, split text on control
        # characters that are handled specially, then save any partial control
        # characters at end of text.
        s = self.saved + s
        parts = self._escape_pat.split(s)
        last = parts[-1]
        match = self._partial_pat.search(last)
        if match:
            i = match.start()
            parts[-1], self.saved = last[:i], last[i:]
        else:
            self.saved = ''
        # Loop over alternating plain and control items
        plain = False
        for part in parts:
            plain = not plain
            if plain:
                # If we have plaintext, it is either real plaintext or the
                # internal part of a PS1 shell prompt.
                if part:
                    if self.prompt_type is None:
                        self.cursor = self.write(self.cursor, part)
                        self.move_cursor()
                    else:
                        self.prompt_text += part
            else:
                self.handle_control(now, part)
        if self.out_start_time:
            # currently displaying output
            out_stop_pos = self.cursor
            if out_stop_pos > self.start_pos:
                settings = self.view.settings()
                history = settings.get('gidterm_history')
                history.append(
                    (0, [(self.start_pos, out_stop_pos)], now.timestamp())
                )
                settings.set('gidterm_history', history)
                self.start_pos = out_stop_pos

    def handle_control(self, now, part):
        view = self.view
        ts = now.timestamp()
        if part == '\x07':
            view.set_name('[BASH]')
            sublime.set_timeout(self.unbell, 250)
            return
        if part[0] == '\x08':
            n = len(part)
            if n > self.cursor:
                n = self.cursor
            self.cursor = self.cursor - n
            self.move_cursor()
            return
        if part[0] == '\r':
            if part[-1] == '\n':
                self.cursor = self.write(view.size(), '\n')
                self.move_cursor()
            else:
                # move cursor to start of line
                classification = view.classify(self.cursor)
                if not classification & sublime.CLASS_LINE_START:
                    bol = view.find_by_class(
                        self.cursor,
                        forward=False,
                        classes=sublime.CLASS_LINE_START
                    )
                    self.cursor = bol
                    self.move_cursor()
            return
        settings = view.settings()
        command = part[-1]
        if command == 'p':
            # prompt
            arg = part[2:-1]
            if arg.endswith('!'):
                prompt_type = arg[0]
                if prompt_type == '0':
                    # trim trailing spaces from input command
                    end = view.size() - 1
                    assert view.substr(end) == '\n'
                    in_end_pos = end
                    while view.substr(in_end_pos - 1) == ' ':
                        in_end_pos -= 1
                    self.delete(sublime.Region(in_end_pos, end))
                    # update history
                    self.in_lines.append(
                        (self.start_pos, in_end_pos)
                    )
                    history = settings.get('gidterm_history')
                    history.append(
                        (1, self.in_lines, ts)
                    )
                    settings.set('gidterm_history', history)
                    self.in_lines = None
                    self.cursor = self.start_pos = view.size()
                    self.move_cursor()
                    self.out_start_time = now
                    return
                elif prompt_type == '2':
                    self.in_lines.append(
                        (self.start_pos, self.cursor)
                    )
                    self.scope = 'sgr.magenta-on-default'
                    self.cursor = self.write(self.cursor, '> ')
                    self.scope = None
                    self.start_pos = self.cursor
                    self.move_cursor()
                    return
            else:
                assert self.prompt_type is None, self.prompt_type
                self.prompt_type = arg
                self.prompt_text = ''
                return
        elif command == '~':
            # end prompt
            assert self.prompt_type == '1', self.prompt_type
            ps1, status, virtualenv, pwd = self.prompt_text.split('@', 3)
            input_ts = now
            cursor = view.size()
            if self.out_start_time is not None:
                # currently displaying output
                if cursor > self.start_pos:
                    history = settings.get('gidterm_history')
                    history.append((0, [(self.start_pos, cursor)], ts))
                    settings.set('gidterm_history', history)
                col = view.rowcol(cursor)[1]
                if col != 0:
                    cursor = self.write(cursor, '\n')
                if status == '0':
                    self.scope = 'sgr.green-on-default'
                else:
                    self.scope = 'sgr.red-on-default'
                cursor = self.write(cursor, status)
                info = _exit_status_info.get(status)
                if info:
                    self.scope = 'sgr.yellow-on-default'
                    cursor = self.write(cursor, info)
                if col == 0:
                    self.scope = 'sgr.green-on-default'
                else:
                    self.scope = 'sgr.red-on-default'
                cursor = self.write(cursor, '\u23ce')
                self.scope = None
                runtime = (input_ts - self.out_start_time)
                s = int(round(runtime.total_seconds()))
                td = timedelta(seconds=s)
                cursor = self.write(cursor, ' {}'.format(td))
                if virtualenv:
                    self.scope = 'sgr.magenta-on-default'
                    cursor = self.write(cursor, ' {}'.format(
                        os.path.basename(virtualenv)
                    ))
                    self.scope = None
                # Reset the output timestamp to None so that
                # pressing enter for a blank line does not show
                # an updated time since run
                self.out_start_time = None
            cursor = self.write(cursor, '\n')
            self.scope = 'sgr.brightblack-on-default'
            cursor = self.write(cursor, '[{}]'.format(pwd))
            self.scope = 'sgr.magenta-on-default'
            self.cursor = self.write(cursor, '\n{} '.format(ps1))
            self.scope = None
            self.start_pos = self.cursor
            self.in_lines = []
            self.move_cursor()
            self.prompt_type = None
            return
        elif command == 'm':
            arg = part[2:-1]
            if not arg:
                self.scope = None
                return
            fg = 'default'
            bg = 'default'
            nums = arg.split(';')
            i = 0
            while i < len(nums):
                num = nums[i]
                if num in ('0', '00'):
                    fg = 'default'
                    bg = 'default'
                elif num == '30':
                    fg = 'black'
                elif num == '31':
                    fg = 'red'
                elif num == '32':
                    fg = 'green'
                elif num == '33':
                    fg = 'yellow'
                elif num == '34':
                    fg = 'blue'
                elif num == '35':
                    fg = 'cyan'
                elif num == '36':
                    fg = 'magenta'
                elif num == '37':
                    fg = 'white'
                elif num == '38':
                    i += 1
                    selector = nums[i]
                    if selector == '2':
                        # r, g, b
                        i += 3
                    elif selector == '5':
                        # 8-bit
                        idx = int(nums[i + 1])
                        if idx < 8:
                            nums[i + 1] = str(30 + idx)
                        elif idx < 16:
                            nums[i + 1] = str(90 + idx - 8)
                        else:
                            i += 1
                elif num == '39':
                    fg = 'default'
                elif num == '40':
                    bg = 'black'
                elif num == '41':
                    bg = 'red'
                elif num == '42':
                    bg = 'green'
                elif num == '43':
                    bg = 'yellow'
                elif num == '44':
                    bg = 'blue'
                elif num == '45':
                    bg = 'cyan'
                elif num == '46':
                    bg = 'magenta'
                elif num == '47':
                    bg = 'white'
                elif num == '48':
                    i += 1
                    selector = nums[i]
                    if selector == '2':
                        # r, g, b
                        i += 3
                    elif selector == '5':
                        # 8-bit
                        idx = int(nums[i + 1])
                        if idx < 8:
                            nums[i + 1] = str(40 + idx)
                        elif idx < 16:
                            nums[i + 1] = str(100 + idx - 8)
                        else:
                            i += 1
                elif num == '49':
                    bg = 'default'
                elif num == '90':
                    fg = 'brightblack'
                elif num == '91':
                    fg = 'brightred'
                elif num == '92':
                    fg = 'brightgreen'
                elif num == '93':
                    fg = 'brightyellow'
                elif num == '94':
                    fg = 'brightblue'
                elif num == '95':
                    fg = 'brightcyan'
                elif num == '96':
                    fg = 'brightmagenta'
                elif num == '97':
                    fg = 'brightwhite'
                elif num == '100':
                    bg = 'brightblack'
                elif num == '101':
                    bg = 'brightred'
                elif num == '102':
                    bg = 'brightgreen'
                elif num == '103':
                    bg = 'brightyellow'
                elif num == '104':
                    bg = 'brightblue'
                elif num == '105':
                    bg = 'brightcyan'
                elif num == '106':
                    bg = 'brightmagenta'
                elif num == '107':
                    bg = 'brightwhite'
                else:
                    print('Unhandled SGR code: {} in {}'.format(
                        num, part
                    ))
                i += 1
            scope = 'sgr.{}-on-{}'.format(fg, bg)
            if scope == 'sgr.default-on-default':
                scope = None
            self.scope = scope
            return
        elif command == '@':
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            # keep cursor at start
            self.insert(self.cursor, ' ' * n)
            return
        elif command == 'C':
            # right
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            self.cursor = min(self.cursor + n, view.size())
            self.move_cursor()
            return
        elif command == 'D':
            # left
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            self.cursor = max(self.cursor - n, 0)
            self.move_cursor()
            return
        elif command == 'K':
            arg = part[2:-1]
            if not arg or arg == '0':
                # clear to end of line
                classification = view.classify(self.cursor)
                if not classification & sublime.CLASS_LINE_END:
                    eol = view.find_by_class(
                        self.cursor,
                        forward=True,
                        classes=sublime.CLASS_LINE_END
                    )
                    self.erase(sublime.Region(self.cursor, eol))
                    self.move_cursor()
                return
            elif arg == '1':
                # clear to start of line
                classification = view.classify(self.cursor)
                if not classification & sublime.CLASS_LINE_START:
                    bol = view.find_by_class(
                        self.cursor,
                        forward=False,
                        classes=sublime.CLASS_LINE_START
                    )
                    self.erase(sublime.Region(bol, self.cursor))
                return
            elif arg == '2':
                # clear line
                classification = view.classify(self.cursor)
                if classification & sublime.CLASS_LINE_START:
                    bol = self.cursor
                else:
                    bol = view.find_by_class(
                        self.cursor,
                        forward=False,
                        classes=sublime.CLASS_LINE_START
                    )
                if classification & sublime.CLASS_LINE_END:
                    eol = self.cursor
                else:
                    eol = view.find_by_class(
                        self.cursor,
                        forward=True,
                        classes=sublime.CLASS_LINE_END
                    )
                self.erase(sublime.Region(bol, eol))
                self.move_cursor()
                return
        elif command == 'P':
            # delete n
            n = int(part[2:-1])
            end = self.cursor + n
            region = sublime.Region(self.cursor, end)
            self.delete(region)
            return
        elif command in ('A', 'B', 'E', 'F', 'G', 'H', 'J', 'f'):
            # we don't handle other cursor movements, since we lie
            # about the screen width, so apps will get confused. We
            # ensure we are at the start of a line when we see them.
            pos = view.size()
            col = view.rowcol(pos)[1]
            if col != 0:
                pos = self.write(pos, '\n')
            self.cursor = pos
            self.move_cursor()
            return
        print('gidterm: [WARN] unknown control: {!r}'.format(part))
        self.cursor = self.write(self.cursor, part)
        self.move_cursor()

    def loop(self):
        if _shellmap.get(self.view.id()) == self:
            if not self.shell.ready():
                sublime.set_timeout(self.loop, 50)
            else:
                s = self.shell.receive()
                if s:
                    now = datetime.now(timezone.utc)
                    self.handle_output(s, now)
                    sublime.set_timeout(self.loop, 1)
                else:
                    self.shell.close()
        else:
            self.shell.close()


class GidtermCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        filename = self.view.file_name()
        if filename is None:
            cwd = os.environ['HOME']
        else:
            cwd = os.path.dirname(filename)
        window = self.view.window()
        view = window.new_file()
        window.focus_view(view)
        GidtermShell(view, cwd)


class GidtermInputCommand(sublime_plugin.TextCommand):

    def run(self, edit, key):
        shell = _shellmap.get(self.view.id())
        if shell:
            self.view.settings().set('gidterm_follow', True)
            shell.send(key)
        else:
            print('disconnected')


_follow_map = {
    'escape': '\x1b\x1b',
    'up': '\x1b[A',
    'down': '\x1b[B',
    'right': '\x1b[C',
    'left': '\x1b[D',
    'insert': '\x1b[2~',
    'delete': '\x1b[3~',
    'ctrl+@': '\x00',
    'ctrl+a': '\x01',
    'ctrl+b': '\x02',
    'ctrl+c': '\x03',
    'ctrl+d': '\x04',
    'ctrl+e': '\x05',
    'ctrl+f': '\x06',
    'ctrl+g': '\x07',
    'ctrl+h': '\x08',
    'ctrl+i': '\x09',
    'ctrl+j': '\x10',
    'ctrl+k': '\x11',
    'ctrl+l': '\x12',
    'ctrl+m': '\x13',
    'ctrl+n': '\x14',
    'ctrl+o': '\x15',
    'ctrl+p': '\x16',
    'ctrl+q': '\x17',
    'ctrl+r': '\x18',
    'ctrl+s': '\x19',
    'ctrl+t': '\x20',
    'ctrl+u': '\x21',
    'ctrl+v': '\x22',
    'ctrl+w': '\x23',
    'ctrl+x': '\x24',
    'ctrl+y': '\x25',
    'ctrl+z': '\x26',
}


class GidtermFollowingCommand(sublime_plugin.TextCommand):

    def run(self, edit, key):
        view = self.view
        shell = _shellmap.get(view.id())
        if shell:
            s = _follow_map.get(key)
            if s is None:
                print('unexpected follow key: {}'.format(key))
            else:
                shell.send(s)
        else:
            print('disconnected')


_follow_escape = {
    'home':
        lambda view: view.run_command(
            'move_to', {"to": "bol", "extend": False}
        ),
    'end': lambda view: view.run_command(
            'move_to', {"to": "eol", "extend": False}
        ),
    'pagedown':
        lambda view: view.run_command(
            'move', {"by": "pages", "forward": True}
        ),
    'pageup':
        lambda view: view.run_command(
            'move', {"by": "pages", "forward": False}
        ),
    'ctrl+home': lambda view: view.run_command(
            'move_to', {"to": "bof", "extend": False}
        ),
    'ctrl+end': lambda view: view.run_command(
            'move_to', {"to": "eof", "extend": False}
        ),
    'shift+home': lambda view: view.run_command(
            'move_to', {"to": "bol", "extend": True}
        ),
    'shift+end': lambda view: view.run_command(
            'move_to', {"to": "eol", "extend": True}
        ),
    'shift+ctrl+home': lambda view: view.run_command(
            'move_to', {"to": "bof", "extend": True}
        ),
    'shift+ctrl+end': lambda view: view.run_command(
            'move_to', {"to": "eof", "extend": True}
        ),
    'shift+ctrl+pageup': lambda view: view.run_command(
            'gidterm_move_to', {"forward": False}
        ),
    'shift+ctrl+pagedown': lambda view: view.run_command(
            'gidterm_move_to', {"forward": True}
        ),
}


class GidtermEscapeCommand(sublime_plugin.TextCommand):

    def run(self, edit, key):
        action = _follow_escape.get(key)
        if action is None:
            print('unexpected escape key: {}'.format(key))
        else:
            self.view.settings().set('gidterm_follow', False)
            action(self.view)


class GidtermEditingCommand(sublime_plugin.TextCommand):

    def run(self, edit, key):
        view = self.view
        shell = _shellmap.get(view.id())
        if shell:
            if key == 'insert':
                buf = ''.join(view.substr(region) for region in view.sel())
                view.settings().set('gidterm_follow', True)
                shell.move_cursor()
                if shell.in_lines is not None:
                    buf = '\b' * (view.size() - shell.start_pos) + buf
                shell.send(buf)
            elif key == 'delete':
                view.settings().set('gidterm_follow', True)
                shell.move_cursor()
                if shell.in_lines is not None:
                    buf = '\b' * (view.size() - shell.start_pos)
                    shell.send(buf)
            elif key == 'ctrl+v':
                view.settings().set('gidterm_follow', True)
                shell.move_cursor()
                buf = sublime.get_clipboard()
                if buf:
                    shell.send(buf)
            else:
                print('unexpected editing key: {}'.format(key))
        else:
            print('disconnected')


class GidtermMoveToCommand(sublime_plugin.TextCommand):

    def run(self, edit, forward):
        view = self.view
        settings = view.settings()
        sel = view.sel()
        history = settings.get('gidterm_history')
        if forward is True:
            pos = sel[0].end()
            size = len(history)
            index = size // 2
            while history and history[index][1][0][0] < pos:
                history = history[index + 1:]
                size = len(history)
                index = size // 2

            for entry in history:
                if entry[0] == 1 and entry[1][0][0] > pos:
                    sel = view.sel()
                    sel.clear()
                    sel.add_all([sublime.Region(b, e) for (b, e) in entry[1]])
                    view.show(sel)
                    return
            # Set to current command
            settings.set('gidterm_follow', True)
            end = view.size()
            sel = view.sel()
            sel.clear()
            sel.add(end)
            view.show(end)
        else:
            pos = sel[0].begin()
            size = len(history)
            index = size // 2
            while history and history[index][1][-1][1] > pos:
                history = history[:index]
                size = len(history)
                index = size // 2

            for entry in reversed(history):
                if entry[0] == 1 and entry[1][-1][1] < pos:
                    sel = view.sel()
                    sel.clear()
                    sel.add_all([sublime.Region(b, e) for (b, e) in entry[1]])
                    view.show(sel)
                    return


class GidtermListener(sublime_plugin.ViewEventListener):

    @classmethod
    def is_applicable(view, settings):
        if settings.get('is_gidterm'):
            return True
        return False

    @classmethod
    def applies_to_primary_view_only(view):
        return False

    def on_close(self):
        view_id = self.view.id()
        shell = _shellmap.get(view_id)
        if shell:
            del _shellmap[view_id]

    def on_selection_modified(self):
        shell = _shellmap.get(self.view.id())
        if shell:
            shell.handle_selection_modified()
