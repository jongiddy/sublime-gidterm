import codecs
from datetime import datetime, timedelta, timezone
import os
import pty
import re
import select
import signal
import subprocess
import tempfile

import sublime
import sublime_plugin

# Map from Sublime view to GidtermView
_viewmap = {}


profile = br'''
# Read the standard profile, to give a familiar environment.  The profile can
# detect that it is in GidTerm using the `TERM_PROGRAM` environment variable.
export TERM_PROGRAM=Sublime-GidTerm
if [ -r ~/.profile ]; then . ~/.profile; fi

# Replace the settings needed for GidTerm to work, notably the prompt formats.
export PROMPT_COMMAND='PS1=\\[\\e[1p\\]\\\$@\\[$?@${VIRTUAL_ENV}@\\w\\e[~\\]'
export PROMPT_DIRTRIM=
export PS0='\e[0!p'
export PS2='\e[2!p'
export TERM=ansi

# Set LINES and COLUMNS to a standard size for commands run by the shell to
# avoid tools creating wonky output, e.g. many tools display a completion
# percentage on the right side of the screen.  man pages are formatted to fit
# the width COLUMNS.  Prevent bash from resetting these variables.
#
shopt -u checkwinsize
export COLUMNS=80
export LINES=24

# Avoid paging by using `cat` as the default pager.  This is generally nicer
# because you can scroll and search using Sublime Text.  It's not so great for
# `git log` where you typically only want the first page or two.  To fix this,
# set the Git `pager.log` config variable.  A good configuration for Git is:
#    core.editor=/usr/bin/subl --wait
#    pager.log=/usr/bin/less --quit-if-one-screen
export PAGER=cat

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
            pid, status = os.waitpid(self.pid, 0)
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
        if self.fd is None:
            return False
        if s:
            os.write(self.fd, s.encode('utf8'))
        return True

    def ready(self):
        fd = self.fd
        if fd is None:
            return False
        rfds, wfds, xfds = select.select([fd], [], [], 0)
        return fd in rfds

    def receive(self):
        return self.decoder.decode(os.read(self.fd, 4096))


def get_vcs_branch(d):
    # return branch, and a code for status
    # 2 = dirty
    # 1 = not synced with upstream
    # 0 = synced with upstream
    try:
        out = subprocess.check_output(
            ('git', 'branch', '--contains', 'HEAD'),
            cwd=d,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        ).strip()
        lines = out.split('\n')
        for s in lines:
            if s[0] == '*':
                branch = s[1:].strip()
                break
        else:
            return None, 0
        out = subprocess.check_output(
            ('git', 'status', '--porcelain', '--untracked-files=no'),
            cwd=d,
        )
        for line in out.split(b'\n'):
            state = line[:2]
            if state.strip():
                # any other state apart from '  '
                return branch, 2
        return branch, 0
    except FileNotFoundError:
        # git command not in path
        pass
    except subprocess.CalledProcessError as e:
        if 'not a git repository' in e.output:
            pass
        else:
            print('gidterm: [WARN] {}: {}'.format(e, e.output))
    return None, 0


def timedelta_seconds(td):
    s = int(round(td.total_seconds()))
    return timedelta(seconds=s)


class GidtermView(sublime.View):

    _escape_pat = re.compile(
        r'(\x07|'                                       # BEL
        r'(?:\x08+)|'                                   # BACKSPACE's
        r'(?:\r+\n?)|'                                  # CR's with optional NL
        r'(?:\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]))'  # CSI
    )

    _partial_pat = re.compile(
        r'\x1b(\[[\x30-\x3f]*[\x20-\x2f]*)?$'           # CSI
    )

    def __init__(self, view_id, workdir=None):
        super().__init__(view_id)
        self.set_scratch(True)
        self.set_line_endings('Unix')
        self.set_read_only(True)
        winvar = self.window().extract_variables()
        package = _get_package_location(winvar)
        self.set_syntax_file(os.path.join(package, 'gidterm.sublime-syntax'))
        if workdir is None:
            workdir = winvar.get('folder', os.environ.get('HOME', '/'))
        self.pwd = workdir
        self.ps1 = '$'
        self.set_title()
        settings = self.settings()
        settings.set(
            'color_scheme',
            os.path.join(package, 'gidterm.sublime-color-scheme')
        )
        # prevent ST doing work that doesn't help here
        settings.set('mini_diff', False)
        settings.set('spell_check', False)
        # state
        settings.set('is_gidterm', True)
        settings.set('gidterm_command_history', [])
        settings.set('gidterm_pwd', [(self.size(), workdir)])
        # `cursor` is the location of the input cursor. It is often the end of
        # the doc but may be earlier if the LEFT key is used, or during
        # command history rewriting.
        self.cursor = self.size()
        self.start_pos = self.cursor
        self.in_lines = None
        self.out_start_time = None
        self.prompt_type = None
        self.scope = None
        self.saved = ''
        self.shell = None
        self.disconnected = False
        self.loop_active = False

        _set_terminal_mode(self)
        self.move_cursor()

    def start(self, wait):
        self.shell = Shell()
        self.shell.fork(os.path.expanduser(self.pwd))
        if wait is not None:
            if not self.loop_active:
                self.loop_active = True
                sublime.set_timeout(self.loop, wait)

    def set_title(self, s=''):
        name = self.pwd
        s = str(s)
        if s:
            name = '{} {}'.format(name, s)
        if len(name) > 18:
            name = '{}\u2026{}'.format(self.ps1, name[-17:])
        else:
            alt = '{} {} {}'.format(
                self.ps1, os.path.expanduser(self.pwd), s
            ).rstrip()
            if len(alt) <= 20:
                name = alt
            else:
                name = '{} {}'.format(self.ps1, name)
        self.set_name(name)

    def at_prompt(self):
        return self.in_lines is not None and self.cursor == self.start_pos

    def send(self, s):
        if self.disconnected:
            if s == _terminal_capability_map['cr']:
                self.disconnected = False
                self.shell = Shell()
                self.start(50)
                self.cursor = self.size()
                self.scope = None
                _set_terminal_mode(self)
                self.move_cursor()
        elif self.shell is None:
            _set_browse_mode(self)
            self.scope = 'sgr.red-on-default'
            cursor = self.size()
            if self.rowcol(cursor)[1] != 0:
                cursor = self.write(cursor, '\n')
            self.write(cursor, '(disconnected - press Enter to restart)\n')
            self.disconnected = True
        else:
            if _set_terminal_mode(self):
                self.move_cursor()
            self.shell.send(s)

    def close(self):
        if self.shell is not None:
            self.shell.close()
            self.shell = None

    def insert_text(self, start, text):
        if start == self.size():
            self.run_command(
                'append',
                {'characters': text, 'force': True, 'scroll_to_end': True}
            )
            end = self.size()
        else:
            self.set_read_only(False)
            try:
                self.run_command(
                    'gidterm_insert_text', {'point': start, 'characters': text}
                )
            finally:
                self.set_read_only(True)
            end = start + len(text)

        if self.scope is not None:
            regions = self.get_regions(self.scope)
            if regions and regions[-1].end() == start:
                prev = regions.pop()
                region = sublime.Region(prev.begin(), end)
            else:
                region = sublime.Region(start, end)
            regions.append(region)
            self.add_regions(
                self.scope, regions, self.scope,
                flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT
            )

        return end

    def write(self, start, text):
        if start == self.size():
            self.run_command(
                'append',
                {'characters': text, 'force': True, 'scroll_to_end': True}
            )
            end = self.size()
        else:
            end = start + len(text)
            self.set_read_only(False)
            try:
                self.run_command(
                    'gidterm_replace_text',
                    {'begin': start, 'end': end, 'characters': text}
                )
            finally:
                self.set_read_only(True)

        if self.scope is not None:
            regions = self.get_regions(self.scope)
            if regions and regions[-1].end() == start:
                prev = regions.pop()
                region = sublime.Region(prev.begin(), end)
            else:
                region = sublime.Region(start, end)
            regions.append(region)
            self.add_regions(
                self.scope, regions, self.scope,
                flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT
            )

        return end

    def move_cursor(self):
        follow = self.settings().get('gidterm_follow')
        if follow:
            sel = self.sel()
            sel.clear()
            sel.add(self.cursor)
            self.show(self.cursor)

    def erase(self, begin, end):
        length = end - begin
        if length > 0:
            self.set_read_only(False)
            try:
                self.run_command(
                    'gidterm_replace_text',
                    {
                        'begin': begin,
                        'end': end,
                        'characters': ' ' * length,
                    }
                )
            finally:
                self.set_read_only(True)

    def delete(self, begin, end):
        if begin < end:
            self.set_read_only(False)
            self.run_command(
                'gidterm_erase_text',
                {'begin': begin, 'end': end},
            )
            self.set_read_only(True)

    def at_cursor(self):
        cursor = self.cursor
        for region in self.sel():
            if region.begin() != cursor or region.end() != cursor:
                return False
        return True

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

    def handle_control(self, now, part):
        if part == '\x07':
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
                self.cursor = self.write(self.size(), '\n')
                self.move_cursor()
            else:
                # move cursor to start of line
                classification = self.classify(self.cursor)
                if not classification & sublime.CLASS_LINE_START:
                    bol = self.find_by_class(
                        self.cursor,
                        forward=False,
                        classes=sublime.CLASS_LINE_START
                    )
                    self.cursor = bol
                    self.move_cursor()
            return
        settings = self.settings()
        command = part[-1]
        if command == 'p':
            # prompt
            arg = part[2:-1]
            if arg.endswith('!'):
                prompt_type = arg[0]
                if prompt_type == '0':
                    # trim trailing spaces from input command
                    assert self.cursor == self.size()
                    end = self.size() - 1
                    assert self.substr(end) == '\n'
                    in_end_pos = end
                    while self.substr(in_end_pos - 1) == ' ':
                        in_end_pos -= 1
                    self.delete(in_end_pos, end)
                    # update history
                    assert self.substr(in_end_pos) == '\n'
                    self.in_lines.append(
                        (self.start_pos, in_end_pos + 1)
                    )
                    history = settings.get('gidterm_command_history')
                    history.append(self.in_lines)
                    settings.set('gidterm_command_history', history)
                    self.in_lines = None
                    self.cursor = self.size()
                    self.move_cursor()
                    self.out_start_time = now
                    return
                elif prompt_type == '2':
                    assert self.cursor == self.size()
                    end = self.size() - 1
                    assert self.substr(end) == '\n'
                    self.in_lines.append(
                        (self.start_pos, end)
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
            self.ps1 = ps1
            cursor = self.size()
            col = self.rowcol(cursor)[1]
            if col != 0:
                cursor = self.write(cursor, '\n')
            if pwd != self.pwd:
                history = settings.get('gidterm_pwd')
                history.append((cursor, pwd))
                settings.set('gidterm_pwd', history)
                self.pwd = pwd
            self.set_title()
            if self.out_start_time is not None:
                # just finished displaying output
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
                elapsed = timedelta_seconds(now - self.out_start_time)
                cursor = self.write(cursor, ' {}\n'.format(elapsed))
                # Reset the output timestamp to None so that
                # pressing enter for a blank line does not show
                # an updated time since run
                self.out_start_time = None
            extra_line = False
            if virtualenv:
                self.scope = 'sgr.magenta-on-default'
                cursor = self.write(cursor, '{}'.format(
                    os.path.basename(virtualenv)
                ))
                extra_line = True
            branch, state = get_vcs_branch(os.path.expanduser(pwd))
            if branch:
                if extra_line:
                    cursor = self.write(cursor, ' ')
                if state == 0:
                    self.scope = 'sgr.green-on-default'
                elif state == 1:
                    self.scope = 'sgr.yellow-on-default'
                else:
                    self.scope = 'sgr.red-on-default'
                cursor = self.write(cursor, '{}'.format(
                    branch
                ))
                extra_line = True
            self.scope = None
            if extra_line:
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
                elif num in ('1', '01', '2', '02', '22'):
                    # TODO: handle bold/faint intensity
                    pass
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
                        elif idx >= 254:
                            nums[i + 1] = '15'  # mostly white
                        elif idx >= 247:
                            nums[i + 1] = '7'   # light grey
                        elif idx >= 240:
                            nums[i + 1] = '8'   # dark grey
                        elif idx >= 232:
                            nums[i + 1] = '0'   # mostly black
                        else:
                            assert 16 <= idx <= 231, idx
                            rg, b = divmod(idx, 6)
                            r, g = divmod(idx, 6)
                            r //= 3
                            g //= 3
                            b //= 3
                            x = {
                                (0, 0, 0): '8',
                                (0, 0, 1): '12',
                                (0, 1, 0): '10',
                                (0, 1, 1): '14',
                                (1, 0, 0): '9',
                                (1, 0, 1): '13',
                                (1, 1, 0): '11',
                                (1, 1, 1): '7',
                            }
                            nums[i + 1] = x[(r, g, b)]
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
                        elif idx >= 254:
                            nums[i + 1] = '15'  # mostly white
                        elif idx >= 247:
                            nums[i + 1] = '7'   # light grey
                        elif idx >= 240:
                            nums[i + 1] = '8'   # dark grey
                        elif idx >= 232:
                            nums[i + 1] = '0'   # mostly black
                        else:
                            rg, b = divmod(idx, 6)
                            r, g = divmod(idx, 6)
                            r //= 3
                            g //= 3
                            b //= 3
                            x = {
                                (0, 0, 0): '8',
                                (0, 0, 1): '12',
                                (0, 1, 0): '10',
                                (0, 1, 1): '14',
                                (1, 0, 0): '9',
                                (1, 0, 1): '13',
                                (1, 1, 0): '11',
                                (1, 1, 1): '7',
                            }
                            nums[i + 1] = x[(r, g, b)]
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
            self.insert_text(self.cursor, ' ' * n)
            return
        elif command == 'C':
            # right
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            n = min(n, self.size() - self.cursor)
            self.cursor += n
            # Use the `move` command because `self.move_cursor` does nothing if
            # we just change the value of `self.cursor` without changing text.
            for i in range(n):
                self.run_command(
                    'move', {"by": "characters", "forward": True}
                )
            return
        elif command == 'D':
            # left
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            self.cursor = max(n, self.cursor)
            self.cursor -= n
            # Use the `move` command because `self.move_cursor` does nothing if
            # we just change the value of `self.cursor` without changing text.
            for i in range(n):
                self.run_command(
                    'move', {"by": "characters", "forward": False}
                )
            return
        elif command == 'K':
            arg = part[2:-1]
            if not arg or arg == '0':
                # clear to end of line
                classification = self.classify(self.cursor)
                if not classification & sublime.CLASS_LINE_END:
                    eol = self.find_by_class(
                        self.cursor,
                        forward=True,
                        classes=sublime.CLASS_LINE_END
                    )
                    self.erase(self.cursor, eol)
                    self.move_cursor()
                return
            elif arg == '1':
                # clear to start of line
                classification = self.classify(self.cursor)
                if not classification & sublime.CLASS_LINE_START:
                    bol = self.find_by_class(
                        self.cursor,
                        forward=False,
                        classes=sublime.CLASS_LINE_START
                    )
                    self.erase(bol, self.cursor)
                return
            elif arg == '2':
                # clear line
                classification = self.classify(self.cursor)
                if classification & sublime.CLASS_LINE_START:
                    bol = self.cursor
                else:
                    bol = self.find_by_class(
                        self.cursor,
                        forward=False,
                        classes=sublime.CLASS_LINE_START
                    )
                if classification & sublime.CLASS_LINE_END:
                    eol = self.cursor
                else:
                    eol = self.find_by_class(
                        self.cursor,
                        forward=True,
                        classes=sublime.CLASS_LINE_END
                    )
                self.erase(bol, eol)
                self.move_cursor()
                return
        elif command == 'P':
            # delete n
            n = int(part[2:-1])
            end = self.cursor + n
            self.delete(self.cursor, end)
            return
        elif command in ('A', 'B', 'E', 'F', 'G', 'H', 'J', 'f'):
            # we don't handle other cursor movements, since we lie
            # about the screen width, so apps will get confused. We
            # ensure we are at the start of a line when we see them.
            pos = self.size()
            col = self.rowcol(pos)[1]
            if col != 0:
                pos = self.write(pos, '\n')
            self.cursor = pos
            self.move_cursor()
            return
        print('gidterm: [WARN] unknown control: {!r}'.format(part))
        self.cursor = self.write(self.cursor, part)
        self.move_cursor()

    def once(self):
        if self.shell is not None:
            if self.shell.ready():
                s = self.shell.receive()
                if s:
                    now = datetime.now(timezone.utc)
                    if self.out_start_time is not None:
                        elapsed = timedelta_seconds(
                            now - self.out_start_time
                        )
                        self.set_title(elapsed)
                    self.handle_output(s, now)
                    return True
            else:
                now = datetime.now(timezone.utc)
                if self.out_start_time is not None:
                    elapsed = timedelta_seconds(now - self.out_start_time)
                    self.set_title(elapsed)
                return False

        return None

    def loop(self):
        try:
            next = self.once()
            if next is True:
                sublime.set_timeout(self.loop, 1)
            elif next is False:
                sublime.set_timeout(self.loop, 50)
            else:
                assert next is None, next
                self.close()
                self.loop_active = False
                _set_browse_mode(self)
        except Exception:
            self.close()
            self.loop_active = False
            _set_browse_mode(self)
            raise


def _set_browse_mode(view):
    settings = view.settings()
    follow = settings.get('gidterm_follow')
    if follow:
        settings.set('gidterm_follow', False)
        settings.set('block_caret', False)
        settings.set('caret_style', 'blink')
        view.set_status('gidterm_mode', 'Browse mode')
    return follow


def _set_terminal_mode(view):
    settings = view.settings()
    follow = settings.get('gidterm_follow')
    if not follow:
        settings.set('gidterm_follow', True)
        settings.set('block_caret', True)
        settings.set('caret_style', 'solid')
        view.set_status('gidterm_mode', 'Terminal mode')
    return not follow


def _get_package_location(winvar):
    packages = winvar['packages']
    this_package = os.path.dirname(__file__)
    assert this_package.startswith(packages)
    unwanted = os.path.dirname(packages)
    # add one to remove pathname delimiter /
    return this_package[len(unwanted) + 1:]


class GidtermInsertTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, point, characters):
        self.view.insert(edit, point, characters)


class GidtermReplaceTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, begin, end, characters):
        region = sublime.Region(begin, end)
        self.view.replace(edit, region, characters)


class GidtermEraseTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, begin, end):
        region = sublime.Region(begin, end)
        self.view.erase(edit, region)


class GidtermCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        filename = self.view.file_name()
        if filename is None:
            pwd = None
        else:
            pwd = os.path.dirname(filename)
        view = window.new_file()
        window.focus_view(view)
        gview = GidtermView(view.id(), pwd)
        _viewmap[view.id()] = gview
        gview.start(100)


def get_gidterm_view(view):
    view_id = view.id()
    gview = _viewmap.get(view_id)
    if gview is None:
        history = view.settings().get('gidterm_pwd')
        pwd = history[-1][1]
        gview = GidtermView(view_id, pwd)
        _viewmap[view_id] = gview
    return gview


class GidtermSendCommand(sublime_plugin.TextCommand):

    def run(self, edit, characters):
        get_gidterm_view(self.view).send(characters)


_terminal_capability_map = {
    'cr': '\r',             # CR
    'esc': '\x1b\x1b',      # escape
    'ht': '\t',             # tab
    'kbs': '\b',            # backspace
    'kcbt': '\x1b[Z',       # shift-tab
    'kcuu1': '\x1b[A',      # cursor-up
    'kcud1': '\x1b[B',      # cursor-down
    'kcuf1': '\x1b[C',      # cursor-right
    'kcub1': '\x1b[D',      # cursor-left
    'kDC': '\x1b[P',        # shift-delete
    'kdch1': '\x1b[3~',     # delete
    'kEND': '',             # shift-end
    'kich1': '\x1b[L',      # insert
    'kHOM': '',             # shift-home
    'khome': '\x1b[H',      # home
    'kLFT': '',             # shift-cursor-left
    'knp': '',              # next-page
    'kpp': '',              # previous-page
    'kRIT': '',             # shift-cursor-right
    'nel': '\r\x1b[S',      # newline
}


class GidtermSendCapCommand(sublime_plugin.TextCommand):

    def run(self, edit, cap):
        view = get_gidterm_view(self.view)
        seq = _terminal_capability_map.get(cap)
        if seq is None:
            print('unexpected terminal capability: {}'.format(cap))
        else:
            view.send(seq)


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
    'shift+pagedown':
        lambda view: view.run_command(
            'move', {"by": "pages", "forward": True, "extend": True}
        ),
    'shift+pageup':
        lambda view: view.run_command(
            'move', {"by": "pages", "forward": False, "extend": True}
        ),
    'shift+ctrl+home': lambda view: view.run_command(
            'move_to', {"to": "bof", "extend": True}
        ),
    'shift+ctrl+end': lambda view: view.run_command(
            'move_to', {"to": "eof", "extend": True}
        ),
    'shift+ctrl+pageup': lambda view: view.run_command(
            'gidterm_select', {"forward": False}
        ),
    'shift+ctrl+pagedown': lambda view: view.run_command(
            'gidterm_select', {"forward": True}
        ),
}


class GidtermEscapeCommand(sublime_plugin.TextCommand):

    def run(self, edit, key):
        view = get_gidterm_view(self.view)
        action = _follow_escape.get(key)
        if action is None:
            print('unexpected escape key: {}'.format(key))
        else:
            _set_browse_mode(view)
            action(view)


class GidtermInsertCommand(sublime_plugin.TextCommand):

    def run(self, edit, strip):
        view = get_gidterm_view(self.view)
        buf = sublime.get_clipboard()
        if strip:
            buf = buf.strip()
        view.send(buf)


class GidtermReplaceCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = get_gidterm_view(self.view)
        buf = sublime.get_clipboard().strip()
        if view.in_lines is not None:
            buf = '\b' * (view.size() - view.start_pos) + buf
        view.send(buf)


class GidtermDeleteCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = get_gidterm_view(self.view)
        if view.in_lines is not None:
            buf = '\b' * (view.size() - view.start_pos)
        view.send(buf)


class GidtermSelectCommand(sublime_plugin.TextCommand):

    def run(self, edit, forward):
        view = get_gidterm_view(self.view)
        settings = view.settings()
        sel = view.sel()
        history = settings.get('gidterm_command_history')
        if forward is True:
            pos = sel[0].end()
            size = len(history)
            index = size // 2
            while history and history[index][0][0] < pos:
                history = history[index + 1:]
                size = len(history)
                index = size // 2

            for entry in history:
                if entry[0][0] > pos:
                    sel = view.sel()
                    sel.clear()
                    sel.add_all([sublime.Region(b, e) for (b, e) in entry])
                    view.show(sel)
                    return
            # Set to current command
            shell = view.shell
            if shell:
                _set_terminal_mode(view)
                view.move_cursor()
            else:
                end = view.size()
                sel = view.sel()
                sel.clear()
                sel.add(end)
                view.show(end)
        else:
            pos = sel[0].begin()
            size = len(history)
            index = size // 2
            while history and history[index][-1][1] > pos:
                history = history[:index]
                size = len(history)
                index = size // 2

            for entry in reversed(history):
                if entry[-1][1] < pos:
                    sel = view.sel()
                    sel.clear()
                    sel.add_all([sublime.Region(b, e) for (b, e) in entry])
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
        gview = _viewmap.get(view_id)
        if gview:
            del _viewmap[view_id]

    def on_selection_modified(self):
        regions = list(self.view.sel())
        if len(regions) != 1 or not regions[0].empty():
            _set_browse_mode(self.view)
