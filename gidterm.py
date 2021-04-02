import codecs
from datetime import datetime, timedelta, timezone
import errno
import os
import pty
import re
import select
import shlex
import signal
import tempfile

import sublime
import sublime_plugin

# Map from Sublime view to GidTerm Tab
_viewmap = {}


_initial_profile = r'''
# Read the standard profile, to give a familiar environment.  The profile can
# detect that it is in GidTerm using the `TERM_PROGRAM` environment variable.
export TERM_PROGRAM=Sublime-GidTerm
if [ -r ~/.profile ]; then . ~/.profile; fi

# Replace the settings needed for GidTerm to work, notably the prompt formats.
PROMPT_DIRTRIM=
_gidterm_ps1 () {
    status=$?
    old_prompt_command=$1
    PS1="\$ ";
    eval "${old_prompt_command}";
    PS1="\\[\\e[1p${status}@\\w\\e[~\\e[5p\\]${PS1}\\[\\e[~\\]";
    tmpfile=${GIDTERM_CACHE}.$$;
    {
        shopt -p &&
        declare -p | grep -v '^declare -[a-qs-z]*r' &&
        declare -f &&
        alias -p;
    } > ${tmpfile} && mv ${tmpfile} ${GIDTERM_CACHE};
}
# The old `PROMPT_COMMAND` may be a function that, on reload, has not been
# declared when `_gidterm_ps1` is being declared.  If `${GIDTERM_PC}` appears
# directly in the `_gidterm_ps1` declaration, the undefined function can cause
# an error. Instead we pass the old `PROMPT_COMMAND` as a parameter.
GIDTERM_PC=${PROMPT_COMMAND:-:}
PROMPT_COMMAND='_gidterm_ps1 "${GIDTERM_PC}"'
PS0='\e[0!p'
PS2='\e[2!p'
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

# Specific configuration to make applications work well with GidTerm
export RIPGREP_CONFIG_PATH=${GIDTERM_CONFIG}/ripgrep
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
        utf8_decoder_factory = codecs.getincrementaldecoder('utf8')
        self.decoder = utf8_decoder_factory(errors='gidterm')

    def __del__(self):
        self.close()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pid is not None:
            pid, status = os.waitpid(self.pid, 0)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                self.pid = None

    def fork(self, workdir, init_file):
        args = [
            'bash', '--rcfile', init_file
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
        try:
            buf = os.read(self.fd, 8192)
        except OSError as e:
            if e.errno == errno.EIO:
                return self.decoder.decode(b'', final=True)
            raise
        return self.decoder.decode(buf, final=not buf)


def timedelta_seconds(seconds):
    s = int(round(seconds))
    return timedelta(seconds=s)


TITLE_LENGTH = 32
PROMPT = '$'
ELLIPSIS = '\u2025'
LONG_ELLIPSIS = '\u2026'


class OutputView(sublime.View):

    def __init__(self, view_id):
        super().__init__(view_id)
        self.cursor = self.home = self.size()
        self.scope = None

    def set_title(self, extra=''):
        extra = str(extra)
        if extra:
            size = TITLE_LENGTH - len(extra) - 1
            name = '{}\ufe19{}'.format(self.get_label(size), extra)
        else:
            name = self.get_label(TITLE_LENGTH)
        self.set_name(name)

    def set_scope(self, scope):
        self.scope = scope

    def move_cursor(self):
        follow = self.settings().get('gidterm_follow')
        if follow:
            sel = self.sel()
            sel.clear()
            sel.add(self.cursor)
            self.show(self.cursor)

    def insert_text(self, text):
        start = self.cursor
        end = start + len(text)
        if start == self.size():
            self.run_command(
                'append',
                {'characters': text, 'force': True, 'scroll_to_end': False}
            )
            if end != self.size():
                print(
                    'gidterm: [WARN] cursor not at end after inserting'
                    ' {!r}'.format(text)
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

        self.cursor = end

    def write(self, text):
        start = self.cursor
        end = start + len(text)
        if start == self.size():
            self.run_command(
                'append',
                {'characters': text, 'force': True, 'scroll_to_end': False}
            )
            if end != self.size():
                print(
                    'gidterm: [WARN] cursor not at end after writing'
                    ' {!r}'.format(text)
                )
                end = self.size()
        else:
            # Overwrite text to end of line, then insert additional text
            classification = self.classify(start)
            if classification & sublime.CLASS_LINE_END:
                replace_end = start
            else:
                replace_end = self.find_by_class(
                    start,
                    forward=True,
                    classes=sublime.CLASS_LINE_END
                )
            if end < replace_end:
                replace_end = end
            self.set_read_only(False)
            try:
                self.run_command(
                    'gidterm_replace_text',
                    {'begin': start, 'end': replace_end, 'characters': text}
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

        self.cursor = end

    def erase(self, begin, end):
        classification = self.classify(begin)
        if classification & sublime.CLASS_LINE_END:
            eol = begin
        else:
            eol = self.find_by_class(
                begin,
                forward=True,
                classes=sublime.CLASS_LINE_END
            )
        if eol <= end:
            self.delete(begin, eol)
        else:
            length = end - begin
            if length > 0:
                self.set_read_only(False)
                try:
                    self.run_command(
                        'gidterm_replace_text',
                        {
                            'begin': begin,
                            'end': end,
                            'characters': '\ufffd' * length,
                        }
                    )
                finally:
                    self.set_read_only(True)

    def delete(self, begin, end):
        if begin < end:
            self.set_read_only(False)
            self.run_command(
                'gidterm_erase_text', {'begin': begin, 'end': end},
            )
            self.set_read_only(True)

    def handle_control(self, part):
        if part == '\x07':
            return
        if part[0] == '\x08':
            n = len(part)
            self.cursor = self.cursor - n
            if self.cursor < self.home:
                self.cursor = self.home
            return
        if part[0] == '\r':
            # move cursor to start of line
            classification = self.classify(self.cursor)
            if not classification & sublime.CLASS_LINE_START:
                bol = self.find_by_class(
                    self.cursor,
                    forward=False,
                    classes=sublime.CLASS_LINE_START
                )
                self.cursor = bol
            return
        if part == '\n':
            row, col = self.rowcol(self.cursor)
            end = self.size()
            maxrow, _ = self.rowcol(end)
            if row == maxrow:
                self.cursor = end
                self.write('\n')
            else:
                row += 1
                cursor = self.text_point(row, col)
                if self.rowcol(cursor)[0] > row:
                    cursor = self.text_point(row + 1, 0) - 1
                self.cursor = cursor
            return
        print('gidterm: [WARN] unknown control: {!r}'.format(part))
        self.write(part)

    def handle_escape(self, part):
        if part[1] != '[':
            assert part[1] in '()*+]', part
            # ignore codeset and set-title
            return
        command = part[-1]
        if command == 'm':
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
                elif num.startswith('1') and len(num) == 2:
                    # TODO: handle these
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
                        continue
                    elif selector == '5':
                        # 8-bit
                        idx = int(nums[i + 1])
                        if idx < 8:
                            nums[i + 1] = str(30 + idx)
                        elif idx < 16:
                            nums[i + 1] = str(90 + idx - 8)
                        elif idx >= 254:
                            nums[i + 1] = '97'  # mostly white
                        elif idx >= 247:
                            nums[i + 1] = '37'   # light grey
                        elif idx >= 240:
                            nums[i + 1] = '90'   # dark grey
                        elif idx >= 232:
                            nums[i + 1] = '30'   # mostly black
                        else:
                            assert 16 <= idx <= 231, idx
                            rg, b = divmod(idx - 16, 6)
                            r, g = divmod(rg, 6)
                            r //= 3
                            g //= 3
                            b //= 3
                            x = {
                                (0, 0, 0): '90',
                                (0, 0, 1): '94',
                                (0, 1, 0): '92',
                                (0, 1, 1): '96',
                                (1, 0, 0): '91',
                                (1, 0, 1): '95',
                                (1, 1, 0): '93',
                                (1, 1, 1): '37',
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
                            nums[i + 1] = '107'  # mostly white
                        elif idx >= 247:
                            nums[i + 1] = '47'   # light grey
                        elif idx >= 240:
                            nums[i + 1] = '100'   # dark grey
                        elif idx >= 232:
                            nums[i + 1] = '40'   # mostly black
                        else:
                            assert 16 <= idx <= 231, idx
                            rg, b = divmod(idx - 16, 6)
                            r, g = divmod(rg, 6)
                            r //= 3
                            g //= 3
                            b //= 3
                            x = {
                                (0, 0, 0): '100',
                                (0, 0, 1): '104',
                                (0, 1, 0): '102',
                                (0, 1, 1): '106',
                                (1, 0, 0): '101',
                                (1, 0, 1): '105',
                                (1, 1, 0): '103',
                                (1, 1, 1): '47',
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
                    print(
                        'gidterm: [WARN] Unhandled SGR code: {} in {}'.format(
                            num, part
                        )
                    )
                i += 1
            scope = 'sgr.{}-on-{}'.format(fg, bg)
            if scope == 'sgr.default-on-default':
                scope = None
            self.scope = scope
            return
        if command == '@':
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            # keep cursor at start
            cursor = self.cursor
            self.insert_text('\ufffd' * n)
            self.cursor = cursor
            return
        if command == 'A':
            # up
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            row, col = self.rowcol(self.cursor)
            row -= n
            if row < 0:
                row = 0
            cursor = self.text_point(row, col)
            if self.rowcol(cursor)[0] > row:
                cursor = self.text_point(row + 1, 0) - 1
            self.cursor = cursor
            return
        if command == 'B':
            # down
            arg = part[2:-1]
            if arg:
                n = int(arg)
            else:
                n = 1
            row, col = self.rowcol(self.cursor)
            row += n
            cursor = self.text_point(row, col)
            if self.rowcol(cursor)[0] > row:
                cursor = self.text_point(row + 1, 0) - 1
            self.cursor = cursor
            return
        if command == 'C':
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
        if command == 'D':
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
        if command in ('H', 'f'):
            # home
            arg = part[2:-1]
            if not arg:
                hrow = 0
                hcol = 0
            elif ';' in arg:
                parts = arg.split(';')
                hrow = int(parts[0]) - 1
                hcol = int(parts[1]) - 1
            else:
                hrow = int(arg) - 1
                hcol = 0
            row, col = self.rowcol(self.home)
            row += hrow
            col += hcol
            cursor = self.text_point(row, col)
            if self.rowcol(cursor)[0] > row:
                cursor = self.text_point(row + 1, 0) - 1
            self.cursor = cursor
            return
        if command == 'K':
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
                return
        if command == 'P':
            # delete n
            n = int(part[2:-1])
            end = self.cursor + n
            self.delete(self.cursor, end)
            return
        if command in ('E', 'F', 'G', 'J'):
            # we don't handle other cursor movements, since we lie
            # about the screen width, so apps will get confused. We
            # ensure we are at the start of a line when we see them.
            pos = self.size()
            col = self.rowcol(pos)[1]
            if col != 0:
                pos = self.write(pos, '\n')
            self.cursor = pos
        print('gidterm: [WARN] unknown escape: {!r}'.format(part))

    def display_status(self, status, ret_scope, elapsed):
        output_end = self.size()
        col = self.rowcol(output_end)[1]
        self.cursor = output_end
        if col != 0:
            self.write('\n')
        if status == '0':
            self.set_scope('sgr.green-on-default')
        else:
            self.set_scope('sgr.red-on-default')
        self.write(status)
        info = _exit_status_info.get(status)
        if info:
            self.set_scope('sgr.yellow-on-default')
            self.write(info)
        self.set_scope(ret_scope)
        self.write('\u23ce')
        self.set_scope(None)
        self.write(' {}\n'.format(elapsed))


class ShellTab(OutputView):

    _escape_pat = re.compile(
        r'('
        r'\x07|'                                        # BEL
        r'\x08+|'                                       # BACKSPACE's
        r'\r+|'                                         # CR's
        r'\n|'                                          # NL
        r'\x1b(?:'                                      # Escapes:
        r'[()*+]B|'                                     # - codeset
        r'\]0;.*?(?:\x07|\x1b\\)|'                      # - set title
        r'\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]'        # - CSI
        r'))'
    )

    _partial_pat = re.compile(
        r'\x1b([()*+]|\](?:0;?)?.*|\[[\x30-\x3f]*[\x20-\x2f]*)?$'
    )

    def __init__(self, view_id):
        super().__init__(view_id)
        _viewmap[view_id] = self
        history = self.settings().get('gidterm_pwd')
        self.pwd = history[-1][1]
        self.command = []
        self.set_title()

        # `cursor` is the location of the input cursor. It is often the end of
        # the doc but may be earlier if the LEFT key is used, or during
        # command history rewriting.
        self.cursor = self.size()
        self.in_lines = None
        self.out_start_time = None
        self.prompt_type = None
        self.scope = None
        self.saved = ''
        self.shell = None
        self.disconnected = False
        self.loop_active = False
        self.buffered = ''

        _set_browse_mode(self)

    def get_label(self, size):
        if size < 3:
            if size == 0:
                return ''
            if size == 1:
                return PROMPT
            if len(self.pwd) <= size - 1:
                return self.pwd + PROMPT
            return ELLIPSIS + PROMPT

        size -= 1  # for PROMPT
        if self.command:
            arg0 = self.command[0]
            if len(self.command) == 1:
                if len(arg0) <= size - 1:
                    # we can fit '> arg0'
                    right = ' ' + arg0
                else:
                    return '{} {}{}'.format(PROMPT, arg0[:size - 2], ELLIPSIS)
            else:
                if len(arg0) <= size - 3:
                    # we can fit '> arg0 ..'
                    right = ' {} {}'.format(arg0[:size - 3], ELLIPSIS)
                else:
                    return '{} {}{}'.format(
                        PROMPT, arg0[:size - 3], LONG_ELLIPSIS
                    )
        else:
            right = ''

        pwd = self.pwd
        parts = pwd.split('/')
        if len(parts) >= 3:
            short = '**/{}'.format(parts[-1])
        else:
            short = pwd
        path_len = min(len(pwd), len(short))
        right_avail = size - path_len
        if len(self.command) > 1 and right_avail > len(right):
            # we have space to expand the args
            full = ' '.join(self.command)
            if len(full) < right_avail:
                right = ' ' + full
            else:
                right = ' {}{}'.format(full[:right_avail - 2], ELLIPSIS)

        size -= len(right)
        if len(pwd) <= size:
            left = pwd
        elif len(short) <= size:
            left = short
            start = parts[:2]
            end = parts[-1]
            parts = parts[2:-1]
            while parts:
                # keep adding components to the end until we reach the capacity
                c = parts.pop() + '/' + end
                if len(c) <= size - 3:
                    left = '**/{}'.format(c)
                    end = c
                    continue
                # once we cannot add whole components to the end, see if we can
                # add whole components to the start.
                if parts:
                    start.append('**')
                else:
                    start.append('*')
                start.append(end)
                c = '/'.join(start)
                if len(c) <= size:
                    left = c
                else:
                    c = start[0] + '/**/' + end
                    if len(c) <= size:
                        left = c
                break
            else:
                # We added everything but the first two path components.
                # We know that the whole path doesn't fit, so check if
                # we can add the first component.
                c = start[0] + '/*/' + end
                if len(c) <= size:
                    left = c
        elif size > 4:
            end = parts[-1]
            left = '**/*{}'.format(end[4 - size:])
        else:
            left = ''
        return '{}{}{}'.format(left, PROMPT, right)

    def disconnect(self):
        if self.shell is not None:
            self.shell.close()
            self.shell = None
        _set_browse_mode(self)

    def send(self, s):
        if self.shell is None:
            self.shell = Shell()
            self.start(50)
            self.buffered = s
        elif self.buffered:
            self.buffered += s
        else:
            self.shell.send(s)

    def start(self, wait):
        settings = self.settings()
        init_file = settings.get('gidterm_init_file')
        if not os.path.exists(init_file):
            init_script = settings.get('gidterm_init', get_initial_profile())
            init_file = create_init_file(init_script)
            settings.set('gidterm_init_file', init_file)
        self.shell = Shell()
        self.shell.fork(os.path.expanduser(self.pwd), init_file)
        _set_terminal_mode(self)
        self.move_cursor()
        if wait is not None:
            if not self.loop_active:
                self.loop_active = True
                sublime.set_timeout(self.loop, wait)

    def at_prompt(self):
        if self.in_lines is None:
            # currently displaying output
            return False
        return self.cursor == self.home

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
            if self.prompt_type is None:
                if plain:
                    if part:
                        self.write(part)
                else:
                    if part[0] == '\x1b':
                        command = part[-1]
                        if command == 'p':
                            self.handle_prompt(part, now)
                        else:
                            self.handle_escape(part)
                    else:
                        self.handle_control(part)
            else:
                if not plain and part == '\x1b[~':
                    self.handle_prompt_end(part, now)
                else:
                    self.prompt_text += part

        self.move_cursor()

    def handle_prompt(self, part, now):
        arg = part[2:-1]
        if arg.endswith('!'):
            # standalone prompt
            prompt_type = arg[0]
            if prompt_type == '0':
                # command input ends, output starts
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
                    (self.home, in_end_pos + 1)
                )
                settings = self.settings()
                history = settings.get('gidterm_command_history')
                history.append(self.in_lines)
                settings.set('gidterm_command_history', history)
                command = '\n'.join(
                    [
                        self.substr(sublime.Region(b, e))
                        for b, e in self.in_lines
                    ]
                )
                words = shlex.split(command.strip())
                if '/' in words[0]:
                    words[0] = words[0].rsplit('/', 1)[-1]
                self.command = words
                self.in_lines = None
                self.cursor = self.size()
                self.home = self.cursor
                self.out_start_time = now
            elif prompt_type == '2':
                # command input continues
                assert self.cursor == self.size()
                end = self.size() - 1
                assert self.substr(end) == '\n'
                self.in_lines.append((self.home, end))
                self.set_scope('sgr.magenta-on-default')
                self.write('> ')
                self.set_scope(None)
                self.home = self.cursor
        else:
            # start of prompt with interpolated text
            assert self.prompt_type is None, self.prompt_type
            self.prompt_type = arg
            self.prompt_text = ''

    def handle_prompt_end(self, part, now):
        # end prompt
        if self.prompt_type == '1':
            # output ends, command input starts
            if self.buffered:
                self.shell.send(self.buffered)
                self.buffered = ''
            status, pwd = self.prompt_text.split('@', 1)
            output_end = self.size()
            col = self.rowcol(output_end)[1]
            if self.cursor == output_end:
                if col == 0:
                    # cursor at end, with final newline
                    ret_scope = 'sgr.green-on-default'
                else:
                    # cursor at end, but no final newline
                    ret_scope = 'sgr.yellow-on-default'
            else:
                # cursor not at end
                ret_scope = 'sgr.red-on-default'
            if self.out_start_time is not None:
                elapsed = timedelta_seconds(
                    (now - self.out_start_time).total_seconds()
                )
            if pwd != self.pwd:
                settings = self.settings()
                history = settings.get('gidterm_pwd')
                history.append((output_end, pwd))
                settings.set('gidterm_pwd', history)
                settings.set('current_working_directory', pwd)  # for GidOpen
                self.pwd = pwd
                # For `cd` avoid duplicating the name in the title to show more
                # of the path. There's an implicit `status == '0'` here, since
                # the directory doesn't change if the command fails.
                if self.command and self.command[0] == 'cd':
                    self.command = []
            if self.out_start_time is None:
                # generally, pressing Enter at an empty command line
                self.command = []
            else:
                # finished displaying output of command
                self.display_status(status, ret_scope, elapsed)
                self.out_start_time = None
            if self.command:
                self.set_title(status)
            else:
                self.set_title()

            self.set_scope(None)
        else:
            assert self.prompt_type == '5', self.prompt_type
            ps1 = self.prompt_text
            parts = self._escape_pat.split(ps1)
            plain = False
            for part in parts:
                plain = not plain
                if plain:
                    if part:
                        self.write(part)
                else:
                    if part[0] == '\x1b':
                        self.handle_escape(part)
                    else:
                        self.handle_control(part)

        self.home = self.cursor
        self.in_lines = []
        self.prompt_type = None

    def set_time(self):
        now = datetime.now(timezone.utc)
        if self.out_start_time is not None:
            elapsed = (now - self.out_start_time).total_seconds()
            if elapsed > 0.2:
                # don't show time immediately, to avoid flashing
                self.set_title(timedelta_seconds(elapsed))
        return now

    def once(self):
        if self.shell is not None:
            if self.shell.ready():
                s = self.shell.receive()
                if s:
                    now = self.set_time()
                    self.handle_output(s, now)
                    return True
            else:
                self.set_time()
                return False

        return None

    def loop(self):
        try:
            next = self.once()
            if next is True:
                sublime.set_timeout(self.loop, 10)
            elif next is False:
                sublime.set_timeout(self.loop, 50)
            else:
                assert next is None, next
                self.disconnect()
                self.loop_active = False
        except Exception:
            self.disconnect()
            self.loop_active = False
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


def create_init_file(contents):
    cachedir = os.path.expanduser('~/.cache/sublime-gidterm/profile')
    os.makedirs(cachedir, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=cachedir, delete=False) as f:
        f.write(contents)
        f.write('declare -- GIDTERM_CACHE="{}"\n'.format(f.name))
        return f.name


def _get_package_location(winvar):
    packages = winvar['packages']
    this_package = os.path.dirname(__file__)
    assert this_package.startswith(packages)
    unwanted = os.path.dirname(packages)
    # add one to remove pathname delimiter /
    return this_package[len(unwanted) + 1:]


def get_initial_profile():
    this_package = os.path.dirname(__file__)
    config = os.path.join(this_package, 'config')
    return 'declare -- GIDTERM_CONFIG="{}"\n'.format(config) + _initial_profile


def create_view(window, pwd, init_script):
    winvar = window.extract_variables()
    package = _get_package_location(winvar)
    if pwd is None:
        pwd = winvar.get('folder', os.environ.get('HOME', '/'))
    view = window.new_file()
    view.set_line_endings('Unix')
    view.set_read_only(True)
    view.set_scratch(True)
    view.set_syntax_file(os.path.join(package, 'gidterm.sublime-syntax'))
    settings = view.settings()
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
    settings.set('gidterm_pwd', [(0, pwd)])
    settings.set('current_working_directory', pwd)  # for GidOpen
    settings.set('gidterm_init_file', create_init_file(init_script))
    return view


class GidtermCommand(sublime_plugin.TextCommand):
    def run(self, edit, pwd=None):
        settings = self.view.settings()
        if settings.get('is_gidterm'):
            # If the current view is a GidTerm, use the same
            # pwd, configuration, and environment
            if pwd is None:
                pwd = settings.get('gidterm_pwd')[-1][1]
            init_file = settings.get('gidterm_init_file')
            with open(init_file) as f:
                init_script = f.read()
        else:
            # If the current view has a filename, use the same
            # pwd. Use the initial configuration and environment.
            if pwd is None:
                filename = self.view.file_name()
                if filename is not None:
                    pwd = os.path.dirname(filename)
            init_script = get_initial_profile()
        window = self.view.window()
        view = create_view(window, pwd, init_script)
        window.focus_view(view)
        view_id = view.id()
        gview = ShellTab(view_id)
        gview.start(100)


def get_gidterm_view(view, start=False):
    view_id = view.id()
    gview = _viewmap.get(view_id)
    if gview is None:
        if view.settings().get('is_gidterm'):
            gview = ShellTab(view_id)
            if start:
                gview.start(50)
        else:
            raise RuntimeError('not a GidTerm')
    return gview


class GidtermSendCommand(sublime_plugin.TextCommand):

    def run(self, edit, characters):
        view = get_gidterm_view(self.view)
        view.send(characters)
        if _set_terminal_mode(view):
            view.move_cursor()


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
    'kDC2': '\x1b[P',       # shift-delete
    'kDN2': '',             # shift-cursor-down
    'kEND2': '',            # shift-End
    'kHOM2': '',            # shift-Home
    'kLFT2': '',            # shift-cursor-left
    'kNXT2': '',            # shift-Page-Down
    'kPRV2': '',            # shift-Page-Up
    'kRIT2': '',            # shift-cursor-right
    'kUP2': '',             # shift-cursor-up
}


class GidtermSendCapCommand(sublime_plugin.TextCommand):

    def run(self, edit, cap, terminal_mode=True):
        view = get_gidterm_view(self.view)
        seq = _terminal_capability_map.get(cap)
        if not seq:
            print(
                'gidterm: [WARN] unexpected terminal capability: {}'.format(
                    cap
                )
            )
        else:
            if terminal_mode:
                view.send(seq)
                if _set_terminal_mode(view):
                    view.move_cursor()
            else:
                _set_browse_mode(view)
                # Unsetting the selection prevents scrolling, but
                # disables navigation (PgUp, etc). Set to just before
                # end to prevent scrolling.
                view.sel().clear()
                view.sel().add(view.size() - 1)
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
            print('gidterm: [WARN] unexpected escape key: {}'.format(key))
        else:
            _set_browse_mode(view)
            action(view)


class GidtermFollowCommand(sublime_plugin.TextCommand):

    def run(self, edit, key):
        view = get_gidterm_view(self.view)
        action = _follow_escape.get(key)
        if action is None:
            print('gidterm: [WARN] unexpected escape key: {}'.format(key))
        else:
            if _set_terminal_mode(view):
                view.move_cursor()
            action(view)


class GidtermInsertCommand(sublime_plugin.TextCommand):

    def run(self, edit, strip):
        view = get_gidterm_view(self.view)
        buf = sublime.get_clipboard()
        if strip:
            buf = buf.strip()
        view.send(buf)
        if _set_terminal_mode(view):
            view.move_cursor()


class GidtermReplaceCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = get_gidterm_view(self.view)
        buf = sublime.get_clipboard().strip()
        if view.in_lines is not None:
            buf = '\b' * (view.size() - view.home) + buf
        view.send(buf)
        if _set_terminal_mode(view):
            view.move_cursor()


class GidtermDeleteCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        view = get_gidterm_view(self.view)
        if view.in_lines is not None:
            buf = '\b' * (view.size() - view.home)
        view.send(buf)
        if _set_terminal_mode(view):
            view.move_cursor()


class GidtermSelectCommand(sublime_plugin.TextCommand):

    def run(self, edit, forward):
        view = get_gidterm_view(self.view)
        if view.in_lines is None:
            # Running this during output causes slowness / hangs
            # e.g. `man bash` followed by Shift-Ctrl-PgUp
            return
        settings = view.settings()
        sel = view.sel()
        history = settings.get('gidterm_command_history')
        if forward is True:
            try:
                pos = sel[0].end()
            except IndexError:
                pos = view.size()
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
            try:
                pos = sel[0].begin()
            except IndexError:
                pos = view.size()
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
        init_file = self.view.settings().get('gidterm_init_file')
        if init_file and os.path.exists(init_file):
            with open(init_file) as f:
                init_script = f.read()
            self.view.settings().set('gidterm_init', init_script)
            os.unlink(init_file)

    def on_selection_modified(self):
        regions = list(self.view.sel())
        if len(regions) != 1 or not regions[0].empty():
            _set_browse_mode(self.view)
