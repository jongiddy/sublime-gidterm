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
export LANG=C.UTF-8
export PROMPT_COMMAND='PS1=[gidterm-input@\\\\D{%Y%m%dT%H%M%S%z}@$?@\\\\w@]'
export PROMPT_DIRTRIM=
export PS0='[gidterm-output@\\D{%Y%m%dT%H%M%S%z}@]'
export PS2='[gidterm-more@]'
export PS3='[gidterm-ps3@]'
export PS4='[gidterm-trace@]'
export TERM=linux
# Keep a large screen size, since Sublime can deal with wrapping/paging
shopt -u checkwinsize
export LINES=32767
export COLUMNS=32767
'''


class Shell:

    def __init__(self):
        self.pid = None
        self.fd = None
        self.path = None

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
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # child
            os.chdir(workdir)
            os.execvp('bash', args)

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
        # TODO: this might split a character
        return os.read(self.fd, 4096).decode('utf8')


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
                raise RuntimeError()
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
        escape_pat = re.compile(r'(\x07|(?:\x08+)|(?:\r+\n?)|(?:\x1b\[[0-9;]*[A-Za-z]))')
        # TODO: catting this file will match the pattern
        prompt_pat = re.compile(r'(\[gidterm-.+?@\])')
        parts = escape_pat.split(s)
        print(parts)
        plain = False
        for part in parts:
            # TODO: we might have a partial escape
            plain = not plain
            if plain:
                if part:
                    texts = prompt_pat.split(part)
                    prompt = False
                    for text in texts:
                        print(texts)
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
                    elif command == 'C':
                        # right
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        if self.insert + n <= self.view.size():
                            self.insert += n
                            self.move_to(self.insert)
                    elif command == 'D':
                        # left
                        arg = part[2:-1]
                        if arg:
                            n = int(arg)
                        else:
                            n = 1
                        if self.insert >= n:
                            self.insert -= n
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
                    sublime.set_timeout(self.loop, 10)
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
        view.set_name('gidterm')
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
    'shift+ctrl+c': '\x03',
    'shift+ctrl+C': '\x03',
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
