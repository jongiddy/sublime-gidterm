# import sublime
import sublime_plugin
import subprocess


class GidtermState:

    def __init__(self, view):
        self.view = view

    def show(self, initial=None):
        if initial is None:
            initial = self.view.settings().get('gidterm_state')
        window = self.view.window()
        window.show_input_panel(
            '>', initial, self.done, self.change, self.cancel
        )

    def append(self, s):
        self.view.run_command(
            "append", {"scroll_to_end": True, "characters": s}
        )

    def change(self, s):
        self.view.settings().set('gidterm_state', s)

    def done(self, s):
        self.view.settings().set('gidterm_state', s)
        self.append("> {}\n".format(s))
        try:
            output = subprocess.check_output(
                s, stderr=subprocess.STDOUT, shell=True, timeout=300
            )
        except subprocess.CalledProcessError as e:
            self.append("{}\nstatus {}\n".format(e.output, e.returncode))
        except subprocess.TimeoutExpired as e:
            self.append("{}\ntimeout\n".format(e.output))
        except Exception as e:
            self.append("> {}\n".format(str(e)))
        else:
            self.append(output.decode('utf-8', 'backslashreplace'))

    def cancel(self):
        pass


class GidtermCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        view = window.new_file()
        view.set_scratch(True)
        settings = view.settings()
        settings.set('is_gidterm', True)
        settings.set('gidterm_state', '')
        window.focus_view(view)
        view.run_command("append", {"characters": "Hello\n"})
        GidtermState(view).show()


class GidtermEnterCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        GidtermState(self.view).show()


class GidtermInsertCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        text = ''.join([view.substr(region) for region in view.sel()])
        GidtermState(view).show(text)


class GidtermDeleteCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        GidtermState(self.view).show('')


class GidtermListener(sublime_plugin.ViewEventListener):

    @classmethod
    def is_applicable(view, settings):
        if settings.get('is_gidterm'):
            return True
        return False

    @classmethod
    def applies_to_primary_view_only(view):
        return False
