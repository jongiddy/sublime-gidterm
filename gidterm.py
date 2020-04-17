import codecs
import datetime
import os
import pty
import re
import select
import tempfile

import sublime
import sublime_plugin


# Map from view id to shell
_shellmap = {}


profile = b'''
if [ -r ~/.profile ]; then . ~/.profile; fi
export PROMPT_COMMAND='PS1=\\\\e[1p\\\\D{%Y%m%dT%H%M%S%z}@$?@\\\\w\\\\e[~'
export PROMPT_DIRTRIM=
export PS0='\\e[0p\\D{%Y%m%dT%H%M%S%z}\\e[~'
export PS2='\\e[2!p'
export PS3='\\e[3!p'
# Don't replace PS4 because it gets used by Terraform without replacing
# \\e with escape, so passes it through undetected.
export TERM=ansi
# Set COLUMNS to a standard size for commands run by the shell to avoid tools
# creating wonky output, e.g. many tools display a completion percentage on the
# right side of the screen
shopt -u checkwinsize
export COLUMNS=80
# Avoid paging by using cat as the default pager
export PAGER=/bin/cat
# Don't add control commands to the history
export HISTIGNORE=${HISTIGNORE:+${HISTIGNORE}:}'*# [@gidterm@]'
'''


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
            'COLUMNS': '32767',
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
        return self.decoder.decode(os.read(self.fd, 8192))


class GidtermShell:

    def __init__(self, view):
        self.view = view
        # `cursor` is the location of the input cursor. It is often the end of
        # the doc but may be earlier if the LEFT key is used, or during
        # command history rewriting.
        self.cursor = view.size()
        self.overwrite = True
        self.output_ts = None
        self.prompt_type = None
        self.scope = None
        self.follow = True
        view.settings().set('gidterm_follow', self.follow)

        shell = Shell()
        _shellmap[view.id()] = shell

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
        if self.follow:
            sel = self.view.sel()
            sel.clear()
            sel.add(self.cursor)
            self.view.show(self.cursor)

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

    def start(self, workdir):
        shell = _shellmap.get(self.view.id())
        if shell:
            shell.fork(workdir)
            sublime.set_timeout(self.loop, 100)

    def unbell(self):
        self.view.set_name('[bash]')

    def handle_output(self, s):
        escape_pat = re.compile(
            r'(\x07|'                               # BEL
            r'(?:\x08+)|'                           # BACKSPACE's
            r'(?:\r+\n?)|'                          # CR's possibly with a NL
            r'(?:\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]))'  # CSI
        )
        view = self.view
        parts = escape_pat.split(s)
        plain = False
        for part in parts:
            # TODO: we might have a partial escape
            plain = not plain
            if plain:
                # If we have plaintext, it is either real plaintext or the
                # internal part of a PS0 or PS1 shell prompt.
                if part:
                    if self.prompt_type is None:
                        self.cursor = self.write(self.cursor, part)
                    else:
                        self.prompt_text += part
            else:
                if part == '\x07':
                    view.set_name('[BASH]')
                    sublime.set_timeout(self.unbell, 250)
                elif part[0] == '\x08':
                    n = len(part)
                    if n > self.cursor:
                        n = self.cursor
                    self.cursor = self.cursor - n
                    self.move_cursor()
                elif part[0] == '\r':
                    if part[-1] == '\n':
                        self.cursor = self.write(view.size(), '\n')
                        self.move_cursor()
                    else:
                        # move cursor to start of line
                        bol = view.find_by_class(
                            self.cursor,
                            forward=False,
                            classes=sublime.CLASS_LINE_START
                        )
                        self.cursor = bol
                        self.move_cursor()
                else:
                    command = part[-1]
                    if command == 'p':
                        # prompt
                        arg = part[2:-1]
                        if arg.endswith('!'):
                            self.cursor = self.write(
                                self.cursor, '<{}>'.format(arg)
                            )
                            self.move_cursor()
                        else:
                            assert self.prompt_type is None, self.prompt_type
                            self.prompt_type = arg
                            self.prompt_text = ''
                        continue
                    elif command == '~':
                        # end prompt
                        if self.prompt_type == '0':
                            ts = self.prompt_text
                            self.output_ts = datetime.datetime.strptime(
                                ts, '%Y%m%dT%H%M%S%z'
                            )
                        else:
                            assert self.prompt_type == '1', self.prompt_type
                            ts, status, workdir = self.prompt_text.split(
                                '@', 2
                            )
                            input_ts = datetime.datetime.strptime(
                                ts, '%Y%m%dT%H%M%S%z'
                            )
                            cursor = view.size()
                            if self.output_ts is not None:
                                col = view.rowcol(cursor)[1]
                                if col != 0:
                                    cursor = self.write(cursor, '\n')
                                if status == '0':
                                    self.scope = 'markup.normal.green'
                                else:
                                    self.scope = 'markup.normal.red'
                                cursor = self.write(cursor, status)
                                if col == 0:
                                    self.scope = 'markup.normal.green'
                                else:
                                    self.scope = 'markup.normal.red'
                                cursor = self.write(cursor, '\u23ce')
                                self.scope = None
                                cursor = self.write(
                                    cursor,
                                    ' {}\n'.format(input_ts - self.output_ts)
                                )
                                # Reset the output timestamp to None so that
                                # pressing enter for a blank line does not show
                                # an updated time since run
                                self.output_ts = None
                            self.scope = 'markup.bright.black'
                            cursor = self.write(cursor, '[{}]'.format(workdir))
                            self.scope = None
                            self.cursor = self.write(cursor, '\n')
                            self.move_cursor()
                        self.prompt_type = None
                        continue
                    elif command == 'm':
                        arg = part[2:-1]
                        nums = arg.split(';')
                        if not arg or arg == '0':
                            self.scope = None
                            continue
                        elif arg == '1':
                            continue
                        elif '10' in nums and '0' in nums:
                            self.scope = None
                            continue
                        elif '30' in nums:
                            self.scope = 'markup.normal.black'
                            continue
                        elif '31' in nums:
                            self.scope = 'markup.normal.red'
                            continue
                        elif '32' in nums:
                            self.scope = 'markup.normal.green'
                            continue
                        elif '33' in nums:
                            self.scope = 'markup.normal.yellow'
                            continue
                        elif '34' in nums:
                            self.scope = 'markup.normal.blue'
                            continue
                        elif '35' in nums:
                            self.scope = 'markup.normal.cyan'
                            continue
                        elif '36' in nums:
                            self.scope = 'markup.normal.magenta'
                            continue
                        elif '37' in nums:
                            self.scope = 'markup.normal.white'
                            continue
                        elif '90' in nums:
                            self.scope = 'markup.bright.black'
                            continue
                        elif '91' in nums:
                            self.scope = 'markup.bright.red'
                            continue
                        elif '92' in nums:
                            self.scope = 'markup.bright.green'
                            continue
                        elif '93' in nums:
                            self.scope = 'markup.bright.yellow'
                            continue
                        elif '94' in nums:
                            self.scope = 'markup.bright.blue'
                            continue
                        elif '95' in nums:
                            self.scope = 'markup.bright.cyan'
                            continue
                        elif '96' in nums:
                            self.scope = 'markup.bright.magenta'
                            continue
                        elif '97' in nums:
                            self.scope = 'markup.bright.white'
                            continue
                    elif command == '@':
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        # keep cursor at start
                        self.insert(self.cursor, ' ' * n)
                        continue
                    elif command == 'C':
                        # right
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        self.cursor = min(self.cursor + n, view.size())
                        self.move_cursor()
                        continue
                    elif command == 'D':
                        # left
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        self.cursor = max(self.cursor - n, 0)
                        self.move_cursor()
                        continue
                    elif command == 'K':
                        arg = part[2:-1]
                        if not arg or arg == '0':
                            # clear to end of line
                            eol = view.find_by_class(
                                self.cursor,
                                forward=True,
                                classes=sublime.CLASS_LINE_END
                            )
                            self.erase(sublime.Region(self.cursor, eol))
                            self.move_cursor()
                            continue
                        elif arg == '1':
                            # clear to start of line
                            bol = view.find_by_class(
                                self.cursor,
                                forward=False,
                                classes=sublime.CLASS_LINE_START
                            )
                            self.erase(sublime.Region(bol, self.cursor))
                            continue
                        elif arg == '2':
                            # clear line
                            bol = view.find_by_class(
                                self.cursor,
                                forward=False,
                                classes=sublime.CLASS_LINE_START
                            )
                            eol = view.find_by_class(
                                self.cursor,
                                forward=True,
                                classes=sublime.CLASS_LINE_END
                            )
                            self.erase(sublime.Region(bol, eol))
                            self.move_cursor()
                            continue
                    elif command == 'P':
                        # delete n
                        n = int(part[2:-1])
                        end = self.cursor + n
                        region = sublime.Region(self.cursor, end)
                        self.delete(region)
                        continue
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
                        continue
                    print('gidterm: [WARN] unknown control: {!r}'.format(part))
                    self.cursor = self.write(self.cursor, part)
                    self.move_cursor()

    def loop(self):
        shell = _shellmap.get(self.view.id())
        if shell:
            if not shell.ready():
                sublime.set_timeout(self.loop, 100)
            else:
                s = shell.receive()
                if s:
                    self.handle_output(s)
                    sublime.set_timeout(self.loop, 1)
                else:
                    shell.close()


class GidtermCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        filename = self.view.file_name()
        if filename is None:
            cwd = os.environ['HOME']
        else:
            cwd = os.path.dirname(filename)
        window = self.view.window()
        view = window.new_file()
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
        window.focus_view(view)
        GidtermShell(view).start(cwd)


_keyin_map = {
    'backspace': '\x08',
    'enter': '\n',
    'tab': '\t',
    'escape': '\x1b\x1b',
    'up': '\x1b[A',
    'down': '\x1b[B',
    'right': '\x1b[C',
    'left': '\x1b[D',
    'shift+ctrl+a': '\x01',
    'shift+ctrl+A': '\x01',
    # shift+ctrl+b - use cursor left key instead
    'shift+ctrl+c': '\x03',
    'shift+ctrl+C': '\x03',
    'shift+ctrl+d': '\x04',
    'shift+ctrl+D': '\x04',
    'shift+ctrl+e': '\x05',
    'shift+ctrl+E': '\x05',
    # shift+ctrl+f - use cursor right key instead
    'shift+ctrl+g': '\x07',
    'shift+ctrl+G': '\x07',
    'shift+ctrl+h': '\x08',
    'shift+ctrl+H': '\x08',
    'shift+ctrl+i': '\x09',
    'shift+ctrl+I': '\x09',
    'shift+ctrl+j': '\x10',
    'shift+ctrl+J': '\x10',
    'shift+ctrl+k': '\x11',
    'shift+ctrl+K': '\x11',
    'shift+ctrl+l': '\x12',
    'shift+ctrl+L': '\x12',
    'shift+ctrl+m': '\x13',
    'shift+ctrl+M': '\x13',
    # shift+ctrl+n - use cursor down key instead
    'shift+ctrl+o': '\x15',
    'shift+ctrl+O': '\x15',
    # shift+ctrl+p - use cursor up key instead
    'shift+ctrl+q': '\x17',
    'shift+ctrl+Q': '\x17',
    'shift+ctrl+r': '\x18',
    'shift+ctrl+R': '\x18',
    # shift+ctrl+s - no replacement
    'shift+ctrl+t': '\x20',
    'shift+ctrl+T': '\x20',
    'shift+ctrl+u': '\x21',
    'shift+ctrl+U': '\x21',
    'shift+ctrl+v': '\x22',
    'shift+ctrl+V': '\x22',
    'shift+ctrl+w': '\x23',
    'shift+ctrl+W': '\x23',
    'shift+ctrl+x': '\x24',
    'shift+ctrl+X': '\x24',
    'shift+ctrl+y': '\x25',
    'shift+ctrl+Y': '\x25',
    'shift+ctrl+z': '\x26',
    'shift+ctrl+Z': '\x26',
    'shift+space': ' ',
}


class GidtermInputCommand(sublime_plugin.TextCommand):

    def run(self, edit, key, ctrl=False, alt=False, shift=False, super=False):
        shell = _shellmap.get(self.view.id())
        if shell:
            s = _keyin_map.get(key, key)
            shell.send(s)
        else:
            print('disconnected')
        return False


class GidtermPasteCommand(sublime_plugin.TextCommand):

    def run(self, edit):
        shell = _shellmap.get(self.view.id())
        if shell:
            buf = sublime.get_clipboard()
            shell.send(buf)
        else:
            print('disconnected')
        return False


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
        del _shellmap[self.view.id()]
