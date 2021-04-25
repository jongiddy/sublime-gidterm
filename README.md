# Sublime GidTerm

Terminal that runs inside Sublime Text. Linux only.

## Install

Clone this repo into your Sublime Text packages repository.

For example:
```
cd ~/.config/sublime-text-3/Packages
git clone git@github.com:jongiddy/sublime-gidterm.git
```

GidTerm is designed to work well with [GidOpen](https://github.com/jongiddy/sublime-gidopen).
GidOpen provides a context menu that appears when you right-click on a path, allowing you to view the path in a tab.
This allows simple access to files from an `ls` command or contained in build error messages.

## Update

Update by pulling the latest version.

```
cd ~/.config/sublime-text-3/Packages/sublime-gidterm
git pull
```

## Use

`Shift-Ctrl-G` in an existing tab to open a bash shell.
If the existing tab contains a file, the shell will be in the same directory.

GidTerm shows a prompt panel for running commands and a read-only display window for viewing output.
Output appears in the display window once it has scrolled off the top of the terminal.

Typing in either view sends text to the terminal, except for control keys.

When focus is in the prompt panel, control keys will perform terminal actions (e.g. the cursor up key will show command history and `Ctrl-C` will send a signal to terminate the running command).

When focus is in the display window, control keys will perform Sublime Text actions (e.g. the cursor up key will move the cursor up one line and `Ctrl-C` will copy the current selection). 

GidTerm supplies some useful key combinations that work in either view:
- `Ctrl-End` to move to end of display window and follow new output
- `Ctrl-Enter` to open and focus in the prompt panel
- `Ctrl-Shift-Enter` to focus in the display window
- `Ctrl-Shift-PageUp` and `Ctrl-Shift-PageDown` to focus in the display window and select the previous or next command respectively
- `Ctrl-Shift-Insert` to insert the clipboard into the terminal with surrounding whitespace removed. To insert the clipboard without trimming whitespace, use `Ctrl-Shift-V` in the prompt panel or `Ctrl-V` in the display window.
