import codecs
from collections import namedtuple
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import html
import os
import pty
import re
from select import select
import shlex
import signal
import tempfile
import traceback

import sublime  # type: ignore
import sublime_plugin  # type: ignore

this_package = os.path.dirname(__file__)
config_dir = os.path.join(this_package, 'config')

terminal_rows = 24
terminal_cols = 80

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
export COLUMNS=%d
export LINES=%d

# Avoid paging by using `cat` as the default pager.  This is generally nicer
# because you can scroll and search using Sublime Text.  For situations where
# the pager is typically used to see the first entries, use command options
# like `git log -n 5` or pipe to `head`.
export PAGER=cat

# Don't add control commands to the history
export HISTIGNORE=${HISTIGNORE:+${HISTIGNORE}:}'*# [@gidterm@]'

# Specific configuration to make applications work well with GidTerm
GIDTERM_CONFIG="%s"
export RIPGREP_CONFIG_PATH=${GIDTERM_CONFIG}/ripgrep
''' % (terminal_cols, terminal_rows, config_dir)


_exit_status_info = {}  # type: dict[str, str]

for name in dir(signal):
    if name.startswith('SIG') and not name.startswith('SIG_'):
        if name in ('SIGRTMIN', 'SIGRTMAX'):
            continue
        try:
            signum = int(getattr(signal, name))
        except Exception:
            pass
        _exit_status_info[str(signum + 128)] = '\U0001f5f2' + name


def warn(message):
    # type: (str) -> None
    print('GidTerm: [WARN] {}'.format(message))


def timedelta_seconds(seconds):
    # type: (float) -> timedelta
    s = int(round(seconds))
    return timedelta(seconds=s)


TITLE_LENGTH = 32
PROMPT = '$'
ELLIPSIS = '\u2025'
LONG_ELLIPSIS = '\u2026'


def _get_package_location(winvar):
    # type: (dict[str, str]) -> str
    packages = winvar['packages']
    this_package = os.path.dirname(__file__)
    assert this_package.startswith(packages)
    unwanted = os.path.dirname(packages)
    # add one to remove pathname delimiter /
    return this_package[len(unwanted) + 1:]


panel_cache = {}  # type: dict[int, DisplayPanel|LivePanel]


def cache_panel(view, panel):
    # type: (sublime.View, DisplayPanel|LivePanel) -> None
    panel_cache[view.id()] = panel


def uncache_panel(view):
    # type: (sublime.View) -> None
    try:
        del panel_cache[view.id()]
    except KeyError:
        warn('panel not found: {}'.format(panel_cache))


def get_panel(view):
    # type: (sublime.View) -> DisplayPanel|LivePanel|None
    panel = panel_cache.get(view.id())
    if panel is None:
        settings = view.settings()
        if settings.get('is_gidterm_display'):
            panel = DisplayPanel(view)
            cache_panel(view, panel)
    return panel


def get_display_panel(view):
    # type: (sublime.View) -> DisplayPanel
    panel = get_panel(view)
    assert isinstance(panel, DisplayPanel)
    return panel


def gidterm_decode_error(e):
    # type: (...) -> tuple[str, int]
    # If text is not Unicode, it is most likely Latin-1. Windows-1252 is a
    # superset of Latin-1 and may be present in downloaded files.
    # TODO: Use the LANG setting to select appropriate fallback encoding
    b = e.object[e.start:e.end]
    try:
        s = b.decode('windows-1252')
    except UnicodeDecodeError:
        # If even that can't decode, fallback to using Unicode replacement char
        s = b.decode('utf8', 'replace')
    warn('{}: replacing {!r} with {!r}'.format(e.reason, b, s.encode('utf8')))
    return s, e.end


codecs.register_error('gidterm', gidterm_decode_error)


class Terminal:

    def __init__(self):
        # type: () -> None
        self.pid = None  # type: int|None
        self.fd = None  # type: int|None
        utf8_decoder_factory = codecs.getincrementaldecoder('utf8')
        self.decoder = utf8_decoder_factory(errors='gidterm')

    def __del__(self):
        # type: () -> None
        self.stop()

    def start(self, workdir, init_file):
        # type: (str, str) -> None
        args = [
            'bash', '--rcfile', init_file
        ]
        env = os.environ.copy()
        env.update({
            # If COLUMNS is the default of 80, the shell will break long
            # prompts over two lines, making them harder to search for. It also
            # allows the shell to use UP control characters to edit lines
            # during command history navigation, which is difficult to replicate
            # correctly. Setting COLUMNS to a very large value avoids these
            # behaviours.
            #
            # When displaying command completion lists, bash pages them based
            # on the LINES variable. A large LINES value avoids paging.
            #
            # Note that we tell bash that we have a very large terminal, then,
            # through the init script, tell applications started by bash that
            # they have a more typical terminal size.
            'COLUMNS': '32767',
            'LINES': '32767',
            'TERM': 'ansi',
        })
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # child
            try:
                os.chdir(os.path.expanduser(workdir))
            except Exception:
                traceback.print_exc()
            os.execvpe('bash', args, env)
        else:
            # Prevent this file descriptor ending up opened in any subsequent
            # child processes, blocking the close(fd) in this process from
            # terminating the shell.
            state = fcntl.fcntl(self.fd, fcntl.F_GETFD)
            fcntl.fcntl(self.fd, fcntl.F_SETFD, state | fcntl.FD_CLOEXEC)

    def stop(self):
        # type: () -> None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pid is not None:
            pid, status = os.waitpid(self.pid, 0)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                self.pid = None

    def send(self, s):
        # type: (str) -> bool
        if self.fd is None:
            return False
        if s:
            os.write(self.fd, s.encode('utf8'))
        return True

    def ready(self):
        # type: () -> bool
        fd = self.fd
        if fd is None:
            return True
        rfds, wfds, xfds = select((fd,), (), (), 0)
        return fd in rfds

    def receive(self):
        # type: () -> str
        fd = self.fd
        if fd is None:
            return ''
        try:
            buf = os.read(fd, 2048)
        except OSError as e:
            if e.errno == errno.EIO:
                return self.decoder.decode(b'', final=True)
            raise
        return self.decoder.decode(buf, final=not buf)


class TerminalOutput:

    # Pattern to match control characters from the terminal that
    # need to be handled specially.
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

    # Pattern to match the prefix of above. If it occurs at the end of
    # text, wait for more text to find escape.
    _partial_pat = re.compile(
        r'\x1b([()*+]|\](?:0;?)?.*|\[[\x30-\x3f]*[\x20-\x2f]*)?$'
    )

    NotReady = namedtuple('NotReady', ())
    Text = namedtuple('Text', 'text')
    Prompt1Starts = namedtuple('Prompt1Starts', ())
    Prompt1Stops = namedtuple('Prompt1Stops', ())
    Prompt2Starts = namedtuple('Prompt2Starts', ())
    Prompt2Stops = namedtuple('Prompt2Stops', ())
    OutputStarts = namedtuple('OutputStarts', ())
    OutputStops = namedtuple('OutputStops', ('status', 'pwd'))
    CursorUp = namedtuple('CursorUp', 'n')
    CursorDown = namedtuple('CursorDown', 'n')
    CursorLeft = namedtuple('CursorLeft', 'n')
    CursorRight = namedtuple('CursorRight', 'n')
    CursorMoveTo = namedtuple('CursorMoveTo', 'row col')
    CursorReturn = namedtuple('CursorReturn', 'n')
    LineFeed = namedtuple('LineFeed', ())
    ClearToEndOfLine = namedtuple('ClearToEndOfLine', ())
    ClearToStartOfLine = namedtuple('ClearToStartOfLine', ())
    ClearLine = namedtuple('ClearLine', ())
    Insert = namedtuple('Insert', 'n')
    Delete = namedtuple('Delete', 'n')
    SelectGraphicRendition = namedtuple('SelectGraphicRendition', ('foreground', 'background'))

    def __init__(self, terminal):
        # type: (Terminal) -> None
        self.saved = ''
        self.prompt_text = ''
        self.in_prompt = None  # type: str|None

        self._csi_map = {
            '@': self.handle_insert,
            'A': self.handle_cursor_up,
            'B': self.handle_cursor_down,
            'C': self.handle_cursor_right,
            'D': self.handle_cursor_left,
            'H': self.handle_cursor_moveto,
            'K': self.handle_clear_line,
            'P': self.handle_delete,
            'f': self.handle_cursor_moveto,
            'm': self.handle_rendition,
        }

        self.iterator = self.loop(terminal)

    def __iter__(self):
        return self.iterator

    def loop(self, terminal):
        # (Terminal) -> Iterator[namedtuple]
        while terminal:
            if terminal.ready():
                s = terminal.receive()
                if s:
                    yield from self.handle_output(s)
                else:
                    # terminal closed output channel
                    terminal = None
            else:
                yield TerminalOutput.NotReady()

    def handle_output(self, text):
        # (str) -> Iterator[namedtuple]
        # Add any saved text from previous iteration, split text on control
        # characters that are handled specially, then save any partial control
        # characters at end of text.
        text = self.saved + text
        parts = self._escape_pat.split(text)
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
            if self.in_prompt is None:
                if plain:
                    if part:
                        yield TerminalOutput.Text(part)
                else:
                    if part[0] == '\x1b':
                        command = part[-1]
                        if command == 'p':
                            yield from self.handle_prompt(part)
                        else:
                            yield from self.handle_escape(part)
                    else:
                        yield from self.handle_control(part)
            else:
                if not plain and part == '\x1b[~':
                    yield from self.handle_prompt_end(part)
                else:
                    self.prompt_text += part

    def handle_prompt(self, part):
        # (str) -> Iterator[namedtuple]
        arg = part[2:-1]
        if arg.endswith('!'):
            # standalone prompt
            in_prompt = arg[0]
            if in_prompt == '0':
                yield TerminalOutput.OutputStarts()
            elif in_prompt == '2':
                # command input continues
                yield TerminalOutput.Prompt2Starts()
                yield TerminalOutput.Text('> ')
                yield TerminalOutput.Prompt2Stops()
        else:
            # start of prompt with interpolated text
            assert self.in_prompt is None, self.in_prompt
            self.in_prompt = arg
            self.prompt_text = ''

    def handle_prompt_end(self, part):
        # (str) -> Iterator[namedtuple]
        if self.in_prompt == '1':
            # output ends, command input starts
            status, pwd = self.prompt_text.split('@', 1)
            yield TerminalOutput.OutputStops(status, pwd)
        else:
            assert self.in_prompt == '5', self.in_prompt
            yield TerminalOutput.Prompt1Starts()
            ps1 = self.prompt_text
            parts = self._escape_pat.split(ps1)
            plain = False
            for part in parts:
                plain = not plain
                if plain:
                    if part:
                        yield TerminalOutput.Text(part)
                else:
                    if part[0] == '\x1b':
                        yield from self.handle_escape(part)
                    else:
                        yield from self.handle_control(part)
            yield TerminalOutput.Prompt1Stops()

        self.in_prompt = None
        self.prompt_text = ''

    def handle_control(self, part):
        # (str) -> Iterator[namedtuple]
        if part == '\x07':
            return
        if part[0] == '\x08':
            n = len(part)
            yield TerminalOutput.CursorLeft(n)
            return
        if part[0] == '\r':
            # move cursor to start of line
            n = len(part)
            yield TerminalOutput.CursorReturn(n)
            return
        if part == '\n':
            yield TerminalOutput.LineFeed()
            return
        warn('unknown control: {!r}'.format(part))

    def handle_escape(self, part):
        # (str) -> Iterator[namedtuple]
        if part[1] != '[':
            assert part[1] in '()*+]', part
            # ignore codeset and set-title
            return
        command = part[-1]
        method = self._csi_map.get(command)
        if method is None:
            warn('Unhandled escape code: {!r}'.format(part))
        else:
            yield from method(part[2:-1])

    def handle_insert(self, arg):
        # (str) -> Iterator[namedtuple]
        if arg:
            n = int(arg)
        else:
            n = 1
        yield TerminalOutput.Insert(n)

    def handle_cursor_up(self, arg):
        # (str) -> Iterator[namedtuple]
        if arg:
            n = int(arg)
        else:
            n = 1
        yield TerminalOutput.CursorUp(n)

    def handle_cursor_down(self, arg):
        # (str) -> Iterator[namedtuple]
        if arg:
            n = int(arg)
        else:
            n = 1
        yield TerminalOutput.CursorDown(n)

    def handle_cursor_right(self, arg):
        # (str) -> Iterator[namedtuple]
        if arg:
            n = int(arg)
        else:
            n = 1
        yield TerminalOutput.CursorRight(n)

    def handle_cursor_left(self, arg):
        # (str) -> Iterator[namedtuple]
        if arg:
            n = int(arg)
        else:
            n = 1
        yield TerminalOutput.CursorLeft(n)

    def handle_cursor_moveto(self, arg):
        # (str) -> Iterator[namedtuple]
        if not arg:
            row = 0
            col = 0
        elif ';' in arg:
            parts = arg.split(';')
            row = int(parts[0]) - 1
            col = int(parts[1]) - 1
        else:
            row = int(arg) - 1
            col = 0
        yield TerminalOutput.CursorMoveTo(row, col)

    def handle_clear_line(self, arg):
        # (str) -> Iterator[namedtuple]
        if not arg or arg == '0':
            # clear to end of line
            yield TerminalOutput.ClearToEndOfLine()
        elif arg == '1':
            # clear to start of line
            yield TerminalOutput.ClearToStartOfLine()
        elif arg == '2':
            # clear line
            yield TerminalOutput.ClearLine()

    def handle_delete(self, arg):
        # (str) -> Iterator[namedtuple]
        n = int(arg)
        yield TerminalOutput.Delete(n)

    def handle_rendition(self, arg):
        # (str) -> Iterator[namedtuple]
        if not arg:
            # ESC[m -> default
            yield TerminalOutput.SelectGraphicRendition('default', 'default')
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
                warn('Unhandled SGR code: {} in {}'.format(num, arg))
            i += 1
        yield TerminalOutput.SelectGraphicRendition(fg, bg)

class CommandHistory:

    def __init__(self, view):
        # type: (sublime.View) -> None
        self.settings = view.settings()
        self.load()

    def load(self):
        # type: () -> None
        # Settings allow None, bool, int, float, str, dict, list (with tuples
        # converted to lists).
        # list[int] is a Region(begin, end) using above types
        # next list is multiple Region's for a multi-line command
        # next list is each command
        self.commands = self.settings.get('gidterm_command_history', [])  # type: list[list[list[int]]]

    def save(self):
        # type: () -> None
        self.settings.set('gidterm_command_history', self.commands)

    def append(self, regions, offset):
        # type: (list[sublime.Region], int) -> None
        command = [[r.begin() + offset, r.end() + offset] for r in regions]
        self.commands.append(command)
        self.save()

    def regions(self, index):
        # type: (int) -> list[sublime.Region]
        return [sublime.Region(c[0], c[1]) for c in self.commands[index]]

    def first_command_before(self, pos):
        # type: (int) -> list[sublime.Region]|None
        commands = self.commands
        low = 0
        if not commands or commands[0][-1][1] > pos:
            return None
        # low is a known valid object and high is index before the first known invalid object
        high = len(commands) - 1
        while high > low:
            index = low + 1 + (high - low) // 2
            command = self.commands[index]
            if command[-1][1] <= pos:
                low = index
            else:
                high = index - 1

        return self.regions(low)

    def first_command_after(self, pos):
        # type: (int) -> list[sublime.Region]|None
        commands = self.commands
        low = 0
        if not commands or commands[-1][0][0] < pos:
            return None
        high = len(self.commands) - 1
        # high is a known valid object and low is index after the first known invalid object
        while high > low:
            index = low + (high - low) // 2
            command = self.commands[index]
            if command[0][0] >= pos:
                high = index
            else:
                low = index + 1

        return self.regions(low)


class DisplayPanel:

    def __init__(self, view):
        # type: (sublime.View) -> None
        self.view = view
        settings = view.settings()
        init_file = settings.get('gidterm_init_file')
        if init_file is None or not os.path.exists(init_file):
            contents = settings.get('gidterm_init_script', _initial_profile)
            init_file = create_init_file(contents)
            settings.set('gidterm_init_file', init_file)
        self.init_file = init_file
        self._live_panel_name = 'gidterm-{}'.format(view.id())

        self.command_history = CommandHistory(view)

        self.set_tab_label('gidterm starting\u2026')

        self.preview_phantoms = sublime.PhantomSet(view, 'preview')

        self.live_panel = LivePanel(
            self,
            self.live_panel_name(),
            self.view.settings().get('current_working_directory'),
            self.init_file,
        )

    def get_color_scheme(self):
        # type: () -> str
        return self.view.settings().get('color_scheme')

    def live_panel_name(self):
        # type: () -> str
        return self._live_panel_name

    def handle_input(self, text):
        # type: (str) -> None
        self.live_panel.handle_input(text)

    def close(self):
        # type: () -> None
        panel_name = self.live_panel_name()
        window = sublime.active_window()
        live_view = window.find_output_panel(panel_name)
        if live_view:
            self.live_panel.close()
            uncache_panel(live_view)
            window.destroy_output_panel(panel_name)
        if os.path.exists(self.init_file):
            with open(self.init_file) as f:
                self.view.settings().set('gidterm_init_script', f.read())
            os.unlink(self.init_file)

    def get_display_panel(self):
        # type: () -> DisplayPanel
        return self

    def setpwd(self, pwd):
        # type: (str) -> None
        settings = self.view.settings()
        settings.set('current_working_directory', pwd)

    def cursor_position(self):
        # type: () -> int|None
        view = self.view
        sel = view.sel()
        if len(sel) != 1:
            return None
        region = sel[0]
        if not region.empty():
            return None
        return region.begin()

    def append_text(self, text, scopes):
        # type: (str, dict[str, list[sublime.Region]]) -> None
        if text:
            text_begin = self.view.size()
            # `force` to override read_only state
            # `scroll_to_end: False` to stay at same position in text.
            self.view.run_command('append', {'characters': text, 'force': True, 'scroll_to_end': False})
            text_end = self.view.size()
            # `scroll_to_end: False` prevents the cursor and text being moved when
            # text is added. However, if the cursor is at the end of the text, this
            # is ignored and the cursor moves and the text scrolls to the end.
            # This is desirable as provides a way to follow the display if desired.
            # As an exception, if `text_begin` is 0, then the cursor stays at start.
            # We override this to follow initially until explict move away from end.
            if text_begin == 0:
                self.view.run_command('gidterm_cursor', {'position': text_end})
            for scope, new_regions in scopes.items():
                regions = self.view.get_regions(scope)
                for new_region in new_regions:
                    # shift region to where text was appended
                    begin = text_begin + new_region.begin()
                    end = text_begin + new_region.end()
                    # trim region to where text was appended
                    if begin >= text_end:
                        continue
                    if end > text_end:
                        end = text_end
                    if regions and regions[-1].end() == begin:
                        # merge into previous region
                        prev = regions.pop()
                        region = sublime.Region(prev.begin(), end)
                    else:
                        region = sublime.Region(begin, end)
                    regions.append(region)
                self.view.add_regions(
                    scope, regions, scope,
                    flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT
                )

    def set_tab_label(self, label):
        # type: (str) -> None
        self.view.set_name(label)

    def focus_display(self):
        # type: () -> None
        window = self.view.window()
        n = window.num_groups()
        for group in range(n):
            if self.view in window.views_in_group(group):
                window.focus_group(group)
                window.focus_view(self.view)
                break

    def focus_live(self):
        # type: () -> None
        panel_name = self.live_panel_name()
        window = self.view.window()
        view = window.find_output_panel(panel_name)
        window.run_command('show_panel', {'panel': 'output.{}'.format(panel_name)})
        window.focus_view(view)

    def show_live(self):
        # type: () -> None
        panel_name = self.live_panel_name()
        window = self.view.window()
        window.run_command('show_panel', {'panel': 'output.{}'.format(panel_name)})

    def add_command_range(self, command_range):
        # type: (list[sublime.Region]) -> None
        self.command_history.append(command_range, self.view.size())

    def add_output(self, text, home, cursor, scopes):
        # type: (str, int, int, dict[str, list[sublime.Region]]) -> None
        self.append_text(text[:home], scopes)
        self.preview(text[home:], cursor)

    def preview(self, text, cursor):
        # type: (str, int) -> None
        def escape(text):
            # type: (str) -> str
            return html.escape(text, quote=False).replace(' ', '&nbsp;')
        text = text.rstrip('\n')  # avoid showing extra line for newline
        if 0 <= cursor <= len(text):
            before = text[:cursor]
            here = text[cursor:cursor + 1] or ' '
            after = text[cursor + 1:]
            text = '%s<u>%s</u>%s' % (escape(before), escape(here), escape(after))
        else:
            text = escape(text)
        end = self.view.size()
        parts = text.split('\n')
        if end == 0:
            # we use LAYOUT_INLINE which needs extra spaces to keep terminal background wide
            parts[0] = parts[0] + '&nbsp;' * 240
        while len(parts) <= terminal_rows:
            parts.append('')
        text = '<br>'.join(parts)
        text = '<body><style>div {background-color: #80808040;}</style><div>%s</div></body>' % text
        if end == 0:
            # Initially, use INLINE to keep preview on first line, not after it
            layout = sublime.LAYOUT_INLINE
        else:
            # Otherwsie, use BLOCK to put after second-last line (assuming last line is empty).
            end = end - 1
            layout = sublime.LAYOUT_BLOCK
        phantom = sublime.Phantom(sublime.Region(end, end), text, layout)
        self.preview_phantoms.update([phantom])

    def next_command(self):
        # type: () -> None
        view = self.view
        sel = view.sel()
        try:
            pos = sel[-1].end()
        except IndexError:
            pos = 0
        regions = self.command_history.first_command_after(pos)
        if regions is None:
            size = view.size()
            regions = [sublime.Region(size, size)]
        sel = view.sel()
        sel.clear()
        sel.add_all(regions)
        view.show(sel)

    def prev_command(self):
        # type: () -> None
        view = self.view
        sel = view.sel()
        try:
            pos = sel[0].begin()
        except IndexError:
            pos = view.size()
        regions = self.command_history.first_command_before(pos)
        if regions is None:
            regions = [sublime.Region(0, 0)]
        sel = view.sel()
        sel.clear()
        sel.add_all(regions)
        view.show(sel)

    def follow(self):
        # type: () -> None
        # Move cursor to end of view, causing window to follow new output
        self.view.run_command('gidterm_cursor', {'position': self.view.size()})


colors = (
    'black',
    'red',
    'green',
    'yellow',
    'blue',
    'magenta',
    'cyan',
    'white',
    'brightblack',
    'brightred',
    'brightgreen',
    'brightyellow',
    'brightblue',
    'brightmagenta',
    'brightcyan',
    'brightwhite',
)


def get_scopes(view):
    # type: (sublime.View) -> dict[str, list[sublime.Region]]
    scopes = {}
    for foreground in colors:
        scope = 'sgr.{}-on-default'.format(foreground)
        regions = view.get_regions(scope)
        if regions:
            scopes[scope] = regions
    for background in colors:
        scope = 'sgr.default-on-{}'.format(background)
        regions = view.get_regions(scope)
        if regions:
            scopes[scope] = regions
    for foreground in colors:
        for background in colors:
            scope = 'sgr.{}-on-{}'.format(foreground, background)
            regions = view.get_regions(scope)
            if regions:
                scopes[scope] = regions
    return scopes


class LivePanel:

    def __init__(self, display_panel, panel_name, pwd, init_file):
        # type: (sublime.View, DisplayPanel, str, str, str) -> None
        self.display_panel = display_panel
        self.panel_name = panel_name
        self.pwd = pwd
        self.init_file = init_file
        self.is_active = False
        view = self.view = self.reset_view(display_panel, panel_name, pwd)
        settings = view.settings()
        settings.set('current_working_directory', pwd)
        settings.set('gidterm_init_file', init_file)

        # State of the output stream
        self.scope = ''  # type: str
        cursor = view.size()  # type: int
        row, col = view.rowcol(cursor)
        if col != 0:
            view.run_command('append', {'characters': '\n', 'force': True, 'scroll_to_end': True})
            cursor = view.size()
            row += 1
        self.cursor = cursor
        self.home_row = row

        # Things that get set during command execution
        self.command_start = None  # type: int|None
        self.command_range = None  # type: list[sublime.Region]|None
        self.command_words = []  # type: list[str]
        self.out_start_time = None  # type: datetime|None
        self.update_running = False

        self.terminal = Terminal()  # type: Terminal|None
        self.terminal.start(self.pwd, self.init_file)
        self.terminal_output = TerminalOutput(self.terminal)
        self.buffered = ''

        sublime.set_timeout(self.wait_for_prompt, 100)

    def close(self):
        # type: () -> None
        if self.terminal:
            self.terminal_closed()

    def setpwd(self, pwd):
        # type: (str) -> None
        self.pwd = pwd
        settings = self.view.settings()
        settings.set('current_working_directory', pwd)
        self.display_panel.setpwd(pwd)

    def get_display_panel(self):
        # type: () -> DisplayPanel
        return self.display_panel

    def set_active(self, is_active):
        # type: (bool) -> None
        self.is_active = is_active

    def focus_display(self):
        # type: () -> None
        self.display_panel.focus_display()

    def set_title(self, extra=''):
        # type: (str) -> None
        if extra:
            size = TITLE_LENGTH - len(extra) - 1
            if size < 1:
                label = extra
            else:
                label = '{}\ufe19{}'.format(self.make_label(size), extra)
        else:
            label = self.make_label(TITLE_LENGTH)
        self.display_panel.set_tab_label(label)

    def make_label(self, size):
        # type: (int) -> str
        pwd = self.pwd
        command_words = self.command_words
        if size < 3:
            if size == 0:
                return ''
            if size == 1:
                return PROMPT
            if len(pwd) < size:
                return pwd + PROMPT
            return ELLIPSIS + PROMPT

        size -= 1  # for PROMPT
        if command_words:
            arg0 = command_words[0]
            if len(command_words) == 1:
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

        parts = pwd.split('/')
        if len(parts) >= 3:
            short = '**/{}'.format(parts[-1])
        else:
            short = pwd
        path_len = min(len(pwd), len(short))
        right_avail = size - path_len
        if len(command_words) > 1 and right_avail > len(right):
            # we have space to expand the args
            full = ' '.join(command_words)
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

    def handle_input(self, text):
        # type: (str) -> None
        if self.terminal is None:
            self.buffered = text
            self.terminal = Terminal()
            self.terminal.start(self.pwd, self.init_file)
            self.terminal_output = TerminalOutput(self.terminal)
            sublime.set_timeout(self.wait_for_prompt, 100)
        elif self.buffered:
            self.buffered += text
        else:
            self.terminal.send(text)

    def reset_view(self, display_panel, panel_name, pwd):
        # type: (DisplayPanel, str, str) -> sublime.View
        window = sublime.active_window()
        view = window.find_output_panel(panel_name)
        if view is not None:
            # uncache first, so event listeners do not get called.
            uncache_panel(view)
            window.destroy_output_panel(panel_name)
        view = window.create_output_panel(panel_name)
        view.set_read_only(True)
        view.set_scratch(True)
        view.set_line_endings('Unix')

        settings = view.settings()
        settings.set('color_scheme', display_panel.get_color_scheme())
        settings.set('block_caret', True)
        settings.set('caret_style', 'solid')
        # prevent ST doing work that doesn't help here
        settings.set('mini_diff', False)
        settings.set('spell_check', False)

        settings.set('is_gidterm', True)
        settings.set('is_gidterm_live', True)
        settings.set('current_working_directory', pwd)

        cache_panel(view, self)

        if self.is_active:
            window.run_command('show_panel', {'panel': 'output.{}'.format(panel_name)})
            window.focus_view(view)

        return view

    def push(self):
        # type: () -> None
        view = self.view
        home = view.text_point(self.home_row, 0)
        scopes = get_scopes(self.view)
        self.display_panel.add_output(
            view.substr(sublime.Region(0, view.size())),
            home,
            self.cursor,
            scopes,
        )
        view.set_read_only(False)
        try:
            view.run_command('gidterm_erase_text', {'begin': 0, 'end': home})
        finally:
            view.set_read_only(True)
        assert self.cursor >= home
        self.cursor -= home
        self.home_row = 0

    def wait_for_prompt(self):
        # type: () -> None
        if self.terminal:
            count = 0
            for t in self.terminal_output:
                if isinstance(t, TerminalOutput.NotReady):
                    sublime.set_timeout(self.wait_for_prompt, 100)
                    break
                if isinstance(t, TerminalOutput.OutputStops):
                    # prompt about to be emitted
                    if self.buffered:
                        self.terminal.send(self.buffered)
                        self.buffered = ''
                    self.set_title()
                    sublime.set_timeout(self.handle_output, 0)
                    break
                count += 1
                if count > 100:
                    # give other events a chance to run
                    sublime.set_timeout(self.wait_for_prompt, 0)
                    break
            else:
                self.terminal_closed()

    def handle_output(self):
        # type: () -> None
        if self.terminal:
            update_preview = False
            view = self.view
            count = 0
            for t in self.terminal_output:
                if isinstance(t, TerminalOutput.NotReady):
                    sublime.set_timeout(self.handle_output, 100)
                    break
                if isinstance(t, TerminalOutput.Prompt1Starts):
                    self.command_start = None
                    assert self.cursor == view.size(), (self.cursor, view.size())
                elif isinstance(t, TerminalOutput.Prompt1Stops):
                    assert self.cursor == view.size()
                    self.command_start = self.cursor
                    self.command_range = []
                    self.scope = ''
                elif isinstance(t, TerminalOutput.Prompt2Starts):
                    assert self.cursor == view.size()
                    end = self.cursor - 1
                    assert view.substr(end) == '\n'
                    assert self.command_range is not None
                    self.command_range.append(sublime.Region(self.command_start, end))
                    self.command_start = None
                    self.scope = 'sgr.magenta-on-default'
                elif isinstance(t, TerminalOutput.Prompt2Stops):
                    assert self.cursor == view.size()
                    assert self.command_start is None
                    self.command_start = self.cursor
                    self.scope = ''
                elif isinstance(t, TerminalOutput.OutputStarts):
                    self.out_start_time = datetime.now(timezone.utc)
                    assert self.cursor == view.size()
                    end = self.cursor - 1
                    assert view.substr(end) == '\n'
                    command_range = self.command_range
                    assert command_range is not None
                    command_range.append(sublime.Region(self.command_start, end))
                    self.command_start = None
                    self.display_panel.add_command_range(command_range)
                    command = '\n'.join(view.substr(region) for region in command_range)
                    self.command_range = None
                    # view = self.view = self.reset_view(self.display_panel, self.panel_name, self.pwd)
                    # Re-add the command without prompts. Note that it has been pushed.
                    # self.append_text(command + '\n')
                    # self.cursor = self.pushed = view.size()
                    # view.add_regions('command', [sublime.Region(0, self.cursor)], 'sgr.default-on-yellow', flags=0)
                    try:
                        words = shlex.split(command.strip())
                    except ValueError as e:
                        # after a PS2 prompt, this indicates the start of a shell interaction
                        # TODO: handle this properly
                        warn(str(e))
                        words = ['shell']
                    if '/' in words[0]:
                        words[0] = words[0].rsplit('/', 1)[-1]
                    self.command_words = words
                    self.set_title(str(timedelta_seconds(0.0)))
                    if not self.update_running:
                        sublime.set_timeout(self.update_elapsed, 1000)
                        self.update_running = True
                elif isinstance(t, TerminalOutput.OutputStops):
                    if self.command_start is None:
                        # end of an executed command
                        status = t.status
                        self.display_status(status)
                        self.home_row, col = view.rowcol(view.size())
                        assert col == 0, col
                        self.push()
                        view = self.view = self.reset_view(self.display_panel, self.panel_name, self.pwd)
                        if t.pwd != self.pwd:
                            self.setpwd(t.pwd)
                            # For `cd` avoid duplicating the name in the title to show more
                            # of the path. There's an implicit `status == '0'` here, since
                            # the directory doesn't change if the command fails.
                            if self.command_words and self.command_words[0] in ('cd', 'popd', 'pushd'):
                                self.command_words.clear()
                                status = ''
                        self.set_title(status)
                        self.command_words = []
                    else:
                        # Pressing Enter without a command or end of a shell
                        # interaction, e.g. Display all possibilities? (y or n)
                        self.set_title()
                        self.command_start = None
                elif isinstance(t, TerminalOutput.Text):
                    self.overwrite(t.text)
                    update_preview = True
                elif isinstance(t, TerminalOutput.CursorUp):
                    row, col = view.rowcol(self.cursor)
                    row -= t.n
                    if row < 0:
                        row = 0
                    cursor = view.text_point(row, col)
                    if view.rowcol(cursor)[0] > row:
                        cursor = view.text_point(row + 1, 0) - 1
                    self.cursor = cursor
                    update_preview = True
                elif isinstance(t, TerminalOutput.CursorDown):
                    row, col = view.rowcol(self.cursor)
                    row += t.n
                    cursor = view.text_point(row, col)
                    if view.rowcol(cursor)[0] > row:
                        cursor = view.text_point(row + 1, 0) - 1
                    self.cursor = cursor
                    update_preview = True
                elif isinstance(t, TerminalOutput.CursorLeft):
                    self.cursor = max(self.cursor - t.n, 0)
                    update_preview = True
                elif isinstance(t, TerminalOutput.CursorRight):
                    self.cursor = min(self.cursor + t.n, view.size())
                    update_preview = True
                elif isinstance(t, TerminalOutput.CursorMoveTo):
                    row = view.rowcol(view.size())[0] - terminal_rows + 1
                    if row < self.home_row:
                        row = self.home_row
                    else:
                        self.home_row = row
                    row += t.row
                    col = t.col
                    cursor = view.text_point(row, col)
                    if view.rowcol(cursor)[0] > row:
                        cursor = view.text_point(row + 1, 0) - 1
                        # This puts cursor at end of line `row`. Maybe add spaces
                        # to get to column `col`?
                    self.cursor = cursor
                    update_preview = True
                elif isinstance(t, TerminalOutput.CursorReturn):
                    # move cursor to start of line
                    classification = view.classify(self.cursor)
                    if not classification & sublime.CLASS_LINE_START:
                        bol = view.find_by_class(
                            self.cursor,
                            forward=False,
                            classes=sublime.CLASS_LINE_START
                        )
                        self.cursor = bol
                    update_preview = True
                elif isinstance(t, TerminalOutput.LineFeed):
                    row, col = view.rowcol(self.cursor)
                    end = view.size()
                    maxrow, _ = view.rowcol(end)
                    if row == maxrow:
                        self.append_text('\n')
                        self.cursor = view.size()
                        new_home_row = row - terminal_rows + 1
                        if new_home_row > self.home_row:
                            self.home_row = new_home_row
                    else:
                        row += 1
                        cursor = view.text_point(row, col)
                        if view.rowcol(cursor)[0] > row:
                            cursor = view.text_point(row + 1, 0) - 1
                        self.cursor = cursor
                    update_preview = True
                elif isinstance(t, TerminalOutput.ClearToEndOfLine):
                    classification = view.classify(self.cursor)
                    if not classification & sublime.CLASS_LINE_END:
                        eol = view.find_by_class(
                            self.cursor,
                            forward=True,
                            classes=sublime.CLASS_LINE_END
                        )
                        self.erase(self.cursor, eol)
                    update_preview = True
                elif isinstance(t, TerminalOutput.ClearToStartOfLine):
                    classification = view.classify(self.cursor)
                    if not classification & sublime.CLASS_LINE_START:
                        bol = view.find_by_class(
                            self.cursor,
                            forward=False,
                            classes=sublime.CLASS_LINE_START
                        )
                        self.erase(bol, self.cursor)
                    update_preview = True
                elif isinstance(t, TerminalOutput.ClearLine):
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
                    self.erase(bol, eol)
                    update_preview = True
                elif isinstance(t, TerminalOutput.Insert):
                    # keep cursor at start
                    cursor = self.cursor
                    self.insert_text('\ufffd' * t.n)
                    self.cursor = cursor
                    update_preview = True
                elif isinstance(t, TerminalOutput.Delete):
                    self.delete(self.cursor, self.cursor + t.n)
                    update_preview = True
                elif isinstance(t, TerminalOutput.SelectGraphicRendition):
                    scope = 'sgr.{}-on-{}'.format(t.foreground, t.background)
                    if scope == 'sgr.default-on-default':
                        scope = ''
                    self.scope = scope
                else:
                    warn('unexpected token: {}'.format(t))
                count += 1
                if count > 100:
                    # give other events a chance to run
                    sublime.set_timeout(self.handle_output, 0)
                    break
            else:
                self.terminal_closed()
            if update_preview:
                self.push()
            if all(region.empty() for region in view.sel()):
                view.run_command('gidterm_cursor', {'position': self.cursor})

    def terminal_closed(self):
        # type: () -> None
        assert self.terminal is not None
        self.terminal.stop()
        self.terminal = None
        self.display_status('DISCONNECTED')
        view = self.view
        self.home_row, col = view.rowcol(view.size())
        assert col == 0, col
        self.push()
        self.view = self.reset_view(self.display_panel, self.panel_name, self.pwd)

    def update_elapsed(self):
        # type: () -> None
        if self.out_start_time is None:
            self.update_running = False
        else:
            now = datetime.now(timezone.utc)
            elapsed = (now - self.out_start_time).total_seconds()
            self.set_title(str(timedelta_seconds(elapsed)))
            sublime.set_timeout(self.update_elapsed, 1000)

    def display_status(self, status):
        # type: (str) -> None
        # finished displaying output of command
        view = self.view
        output_end = view.size()
        col = view.rowcol(output_end)[1]
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
        if col != 0:
            self.append_text('\n')
        if status == '0':
            self.scope = 'sgr.green-on-default'
        else:
            self.scope = 'sgr.red-on-default'
        self.append_text(status)
        info = _exit_status_info.get(status)
        if info:
            self.scope = 'sgr.yellow-on-default'
            self.append_text(info)
        self.scope = ret_scope
        self.append_text('\u23ce')
        self.scope = ''
        if self.out_start_time is None:
            self.append_text('\n')
        else:
            elapsed = timedelta_seconds((datetime.now(timezone.utc) - self.out_start_time).total_seconds())
            self.append_text(' {}\n'.format(elapsed))
        self.out_start_time = None

    def _insert(self, view, start, text):
        # type: (sublime.View, int, str) -> int
        view.set_read_only(False)
        try:
            view.run_command('gidterm_insert_text', {'point': start, 'characters': text})
        finally:
            view.set_read_only(True)
        return start + len(text)

    def insert_text(self, text):
        # type: (str) -> None
        self._write(text, self._insert)

    def _overwrite(self, view, start, text):
        # type: (sublime.View, int, str) -> int
        # Overwrite text to end of line, then insert additional text
        end = start + len(text)
        classification = view.classify(start)
        if classification & sublime.CLASS_LINE_END:
            replace_end = start
        else:
            replace_end = view.find_by_class(start, forward=True, classes=sublime.CLASS_LINE_END)
        if end < replace_end:
            replace_end = end
        view.set_read_only(False)
        try:
            view.run_command('gidterm_replace_text', {'begin': start, 'end': replace_end, 'characters': text})
        finally:
            view.set_read_only(True)
        return end

    def overwrite(self, text):
        # type: (str) -> None
        self._write(text, self._overwrite)

    def append_text(self, text):
        # type: (str) -> None
        view = self.view
        start = view.size()
        end = start + len(text)
        view.run_command('append', {'characters': text, 'force': True, 'scroll_to_end': True})
        if end != view.size():
            warn('cursor not at end after writing {!r} {} {}'.format(text, end, view.size()))
            end = view.size()
        if self.scope:
            regions = view.get_regions(self.scope)
            if regions and regions[-1].end() == start:
                prev = regions.pop()
                region = sublime.Region(prev.begin(), end)
            else:
                region = sublime.Region(start, end)
            regions.append(region)
            view.add_regions(self.scope, regions, self.scope, flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT)

        self.cursor = end

    def _write(self, text, add_text):
        # (str, Callable[[sublime.View, int, str], None]) -> None
        view = self.view
        start = self.cursor
        if start == view.size():
            self.append_text(text)
        else:
            end = add_text(view, start, text)

            if self.scope:
                regions = view.get_regions(self.scope)
                if regions and regions[-1].end() == start:
                    prev = regions.pop()
                    region = sublime.Region(prev.begin(), end)
                else:
                    region = sublime.Region(start, end)
                regions.append(region)
                view.add_regions(self.scope, regions, self.scope, flags=sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT)

            self.cursor = end

    def erase(self, begin, end):
        # type: (int, int) -> None
        # Erase the region without shifting characters after the region. This
        # may require replacing the erased characters with placeholders.
        view = self.view
        classification = view.classify(begin)
        if classification & sublime.CLASS_LINE_END:
            eol = begin
        else:
            eol = view.find_by_class(begin, forward=True, classes=sublime.CLASS_LINE_END)
        if eol <= end:
            self.delete(begin, eol)
        else:
            length = end - begin
            if length > 0:
                view.set_read_only(False)
                try:
                    view.run_command(
                        'gidterm_replace_text', {'begin': begin, 'end': end, 'characters': '\ufffd' * length}
                    )
                finally:
                    view.set_read_only(True)

    def delete(self, begin, end):
        # type: (int, int) -> None
        # Delete the region, shifting any later characters into the space.
        if begin < end:
            view = self.view
            assert begin >= view.text_point(self.home_row, 0)
            view.set_read_only(False)
            try:
                view.run_command('gidterm_erase_text', {'begin': begin, 'end': end})
            finally:
                view.set_read_only(True)
            if self.cursor > end:
                self.cursor -= (end - begin)
            elif self.cursor > begin:
                self.cursor = begin

    def follow(self):
        # type: () -> None
        # move prompt panel cursor to current position
        self.view.run_command('gidterm_cursor', {'position': self.cursor})
        # move display panel cursor to end, causing it to follow output
        self.display_panel.follow()


def create_init_file(contents):
    # type: (str) -> str
    cachedir = os.path.expanduser('~/.cache/sublime-gidterm/profile')
    os.makedirs(cachedir, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=cachedir)
    try:
        contents += 'declare -- GIDTERM_CACHE="%s"\n' % name
        os.write(fd, contents.encode('utf-8'))
    finally:
        os.close(fd)
    return name


class GidtermCommand(sublime_plugin.TextCommand):
    def run(self, edit, pwd=None):
        # type: (...) -> None
        init_script = None
        view = self.view
        settings = view.settings()
        if settings.get('is_gidterm'):
            # If the current view is a GidTerm, use the same
            # pwd, configuration, and environment
            if pwd is None:
                pwd = settings.get('current_working_directory')
            init_file = settings.get('gidterm_init_file')
            with open(init_file, encoding='utf-8') as f:
                init_script = f.read()
        if pwd is None:
            # If the current view has a filename, use the same
            # pwd. Use the initial configuration and environment.
            filename = view.file_name()
            if filename is not None:
                pwd = os.path.dirname(filename)
        if init_script is None:
            init_script = _initial_profile

        window = view.window()
        winvar = window.extract_variables()
        if pwd is None:
            pwd = winvar.get('folder', os.environ.get('HOME', '/'))

        package = _get_package_location(winvar)
        color_scheme = os.path.join(package, 'gidterm.sublime-color-scheme')

        display_view = window.new_file()
        display_view.set_read_only(True)
        display_view.set_scratch(True)
        display_view.set_line_endings('Unix')

        settings = display_view.settings()
        settings.set('color_scheme', color_scheme)
        # prevent ST doing work that doesn't help here
        settings.set('mini_diff', False)
        settings.set('spell_check', False)

        settings.set('is_gidterm', True)
        settings.set('is_gidterm_display', True)
        settings.set('current_working_directory', pwd)
        settings.set('gidterm_init_script', init_script)
        settings.set('gidterm_init_file', create_init_file(init_script))

        display_panel = DisplayPanel(display_view)
        cache_panel(display_view, display_panel)
        window.focus_view(display_view)


class GidtermInsertTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, point, characters):
        # type: (...) -> None
        self.view.insert(edit, point, characters)


class GidtermReplaceTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, begin, end, characters):
        # type: (...) -> None
        region = sublime.Region(begin, end)
        self.view.replace(edit, region, characters)


class GidtermEraseTextCommand(sublime_plugin.TextCommand):
    def run(self, edit, begin, end):
        # type: (...) -> None
        region = sublime.Region(begin, end)
        self.view.erase(edit, region)


class GidtermCursorCommand(sublime_plugin.TextCommand):

    # https://github.com/sublimehq/sublime_text/issues/485#issuecomment-337480388

    def run(self, edit, position):
        # type: (...) -> None
        sel = self.view.sel()
        sel.clear()
        sel.add(position)
        self.view.show(position)


class GidtermFollowCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        # type: (...) -> None
        panel = get_panel(self.view)
        if panel:
            panel.follow()


class GidtermFocusDisplay(sublime_plugin.TextCommand):

    def run(self, edit):
        # type: (...) -> None
        panel = get_panel(self.view)
        if panel:
            panel.focus_display()

class GidtermFocusLive(sublime_plugin.TextCommand):

    def run(self, edit):
        # type: (...) -> None
        get_display_panel(self.view).focus_live()


class GidtermSendCommand(sublime_plugin.TextCommand):

    def run(self, edit, characters):
        # type: (...) -> None
        panel = get_panel(self.view)
        if panel:
            panel.handle_input(characters)


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


class GidtermCapabilityCommand(sublime_plugin.TextCommand):

    def run(self, edit, cap):
        # type: (...) -> None
        characters = _terminal_capability_map.get(cap)
        if characters is None:
            warn('unexpected terminal capability: {}'.format(cap))
            return
        panel = get_panel(self.view)
        if panel:
            panel.handle_input(characters)


class GidtermInsertCommand(sublime_plugin.TextCommand):

    def run(self, edit, strip):
        # type: (...) -> None
        panel = get_panel(self.view)
        if panel is not None:
            buf = sublime.get_clipboard()
            if strip:
                buf = buf.strip()
            panel.handle_input(buf)


class GidtermSelectCommand(sublime_plugin.TextCommand):

    def run(self, edit, forward):
        # type: (...) -> None
        view = self.view
        panel = get_panel(view)
        if panel:
            display_panel = panel.get_display_panel()
            display_panel.focus_display()
            if forward:
                display_panel.next_command()
            else:
                display_panel.prev_command()


class GidtermDisplayListener(sublime_plugin.ViewEventListener):

    @classmethod
    def is_applicable(cls, settings):
        # type: (...) -> bool
        return settings.get('is_gidterm_display', False)

    @classmethod
    def applies_to_primary_view_only(cls):
        # type: (...) -> bool
        return False

    def on_pre_close(self):
        # type: () -> None
        view = self.view
        get_display_panel(view).close()
        uncache_panel(view)


# Panels do not trigger `ViewEventListener` so use `EventListener`
class GidtermLiveListener(sublime_plugin.EventListener):

    def on_activated(self, view):
        # type: (sublime.View) -> None
        if view.settings().get('is_gidterm_live', False):
            panel = get_panel(view)
            if panel is not None:
                assert isinstance(panel, LivePanel)
                panel.set_active(True)

    def on_deactivated(self, view):
        # type: (sublime.View) -> None
        if view.settings().get('is_gidterm_live', False):
            panel = get_panel(view)
            if panel is not None:
                assert isinstance(panel, LivePanel)
                panel.set_active(False)
