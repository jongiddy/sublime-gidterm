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
export PROMPT_COMMAND='PS1=[gidterm-input@\\\\D{%Y%m%dT%H%M%S%z}@$?@\\\\w@]'
export PROMPT_DIRTRIM=
export PS0='[gidterm-output@\\D{%Y%m%dT%H%M%S%z}@]'
export PS2='[gidterm-more@]'
export PS3='[gidterm-ps3@]'
export PS4='[gidterm-trace@]'
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
        # `insert` is the location of the input cursor. It is often the end of
        # the doc but may be earlier if the LEFT key is used, or during
        # command history rewriting.
        self.insert = view.size()
        self.overwrite = True
        self.output_ts = None
        history = []
        settings = view.settings()
        settings.set('is_gidterm', True)
        settings.set('command_history', history)
        settings.set('command_index', 0)
        shell = Shell()
        _shellmap[view.id()] = shell

    def cursor_at_end(self):
        size = self.view.size()
        for region in self.view.sel():
            if region.begin() < size:
                return False
        return True

    def set_selection(self, regions):
        sel = self.view.sel()
        sel.clear()
        sel.add_all(regions)

    def move_to(self, pos):
        sel = self.view.sel()
        sel.clear()
        sel.add(sublime.Region(pos, pos))

    def add(self, s):
        view = self.view
        restore = self.position() == self.insert
        if self.insert == view.size():
            view.set_read_only(False)
            view.run_command(
                "append", {"characters": s, "scroll_to_end": True}
            )
            view.set_read_only(True)
            end = view.size()
            self.insert = end
        else:
            begin = self.insert
            end = self.insert + len(s)
            if self.overwrite:
                region = sublime.Region(begin, end)
            else:
                region = sublime.Region(begin, begin)
            self.set_selection([region])
            view.set_read_only(False)
            view.run_command(
                "insert", {"characters": s}
            )
            view.set_read_only(True)
            self.insert = end
        if restore:
            self.move_to(self.insert)

    def erase(self, region=None):
        view = self.view
        sel = view.sel()
        regions = []
        if region is None:
            for region in view.sel():
                if not region.empty():
                    regions.append(region)
        else:
            if not region.empty():
                regions.append(region)
        if regions:
            sel.clear()
            sel.add_all(regions)
            view.set_read_only(False)
            view.run_command("left_delete")
            view.set_read_only(True)

    def position(self):
        pos = None
        sel = self.view.sel()
        for region in sel:
            if region.empty():
                if pos is None:
                    pos = region.begin()
                else:
                    if pos != region.begin():
                        raise RuntimeError()
            else:
                return None
        if pos is None:
            return self.view.size()
        else:
            return pos

    def selection(self):
        s = ','.join('{}-{}'.format(region.begin(), region.end()) for region in self.view.sel())
        if not s:
            s = 'no selection'
        return s

    def start(self, workdir):
        shell = _shellmap.get(self.view.id())
        if shell:
            shell.fork(workdir)
            sublime.set_timeout(self.loop, 100)

    def handle_output(self, s):
        escape_pat = re.compile(r'(\x07|(?:\x08+)|(?:\r+\n?)|(?:\x1b\[[0-9;]*[^0-9;]))')
        # TODO: catting this file will match the pattern (or running set or env)
        prompt_pat = re.compile(r'(\[gidterm-.+?@\])')
        parts = escape_pat.split(s)
        plain = False
        for part in parts:
            # TODO: we might have a partial escape
            plain = not plain
            if plain:
                if part:
                    texts = prompt_pat.split(part)
                    prompt = False
                    for text in texts:
                        if prompt:
                            # remove [gidterm- ... @]
                            text = text[9:-2]
                            if text.startswith('input'):
                                prefix, ts, status, workdir = text.split('@', 3)
                                assert prefix == 'input', text
                                input_ts = datetime.datetime.strptime(ts, '%Y%m%dT%H%M%S%z')
                                if self.output_ts is None:
                                    self.add('[{}]\n'.format(workdir))
                                else:
                                    self.add(
                                        'status={} time={}\n[{}]\n'.format(
                                            status,
                                            input_ts - self.output_ts,
                                            workdir
                                        )
                                    )
                                    # Reset the output timestamp to None so that pressing enter
                                    # for a blank line does not show an updated time since run
                                    self.output_ts = None
                            elif text.startswith('output'):
                                prefix, ts = text.split('@', 1)
                                assert prefix == 'output', text
                                self.output_ts = datetime.datetime.strptime(ts, '%Y%m%dT%H%M%S%z')
                            else:
                                self.add(text)
                        else:
                            if text:
                                self.add(text)
                        prompt = not prompt
            else:
                if part == '\x07':
                    # how do we notify user?
                    pass
                elif part[0] == '\x08':
                    n = len(part)
                    if n > self.insert:
                        n = self.insert
                    self.insert = self.insert - n
                    self.move_to(self.insert)
                elif part[0] == '\r':
                    if part[-1] == '\n':
                        self.add('\n')
                    else:
                        # move cursor to start of line
                        self.move_to(self.insert)
                        self.view.run_command(
                            "move_to", {"to": "bol", "extend": False}
                        )
                        self.insert = self.position()
                else:
                    command = part[-1]
                    if command == 'm':
                        # ignore color
                        pass
                    elif command == '@':
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        insert = self.insert
                        self.move_to(insert)
                        overwrite = self.overwrite
                        self.overwrite = False
                        self.add(' ' * n)
                        self.overwrite = overwrite
                        self.insert = insert
                    elif command == 'C':
                        # right
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        self.insert = min(self.insert + n, self.view.size())
                        self.move_to(self.insert)
                    elif command == 'D':
                        # left
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        self.insert = max(self.insert - n, 0)
                        self.move_to(self.insert)
                    elif command == 'K':
                        # clear to end of line
                        self.move_to(self.insert)
                        self.view.run_command(
                            "move_to", {"to": "eol", "extend": True}
                        )
                        self.erase()
                    elif command == 'P':
                        # delete n
                        n = int(part[2:-1])
                        end = self.insert + n
                        region = sublime.Region(self.insert, end)
                        self.erase(region)
                    else:
                        print(parts)
                        self.add(part)

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
        view.set_line_endings('Unix')
        view.set_read_only(True)
        window.focus_view(view)
        GidtermShell(view).start(cwd)


_keyin_map = {
    'backspace': '\x08',
    'enter': '\n',
    'tab': '\t',
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
