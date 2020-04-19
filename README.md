# Sublime GidTerm

Terminal that runs inside SublimeText. Alpha quality. Linux only.

## Install

Clone this repo into your SublimeText packages repository.

For example:
```
cd ~/.config/sublime-text-3/Packages
git clone git@github.com:jongiddy/sublime-gidterm.git
```

## Update

Update by pulling the latest version.

```
cd ~/.config/sublime-text-3/Packages/sublime-gidterm
git pull
```

## Use

`Shift-Ctrl-G` in an existing tab to open a bash shell.
If the existing tab contains a file, the shell will be in the same directory.

The shell tab has two modes:

- *terminal mode* where control keys are sent to the terminal. The cursor up key will show command history and `Ctrl-C` will send a signal that will likely terminate the running command.
- *browse mode* where control keys perform Sublime commands. The cursor up key will move the cursor up one line and `Ctrl-C` will copy the current selection.

In terminal mode, the following keys will change to browse mode: `Home`, `End`, `PageUp`, `PageDown`.

`Shift-Ctrl-PageUp` and `Shift-Ctrl-PageDown` will change to browse mode and select the previous or next command respectively.

In browse mode, typing any printing character will change to terminal mode.
Pasting using `Ctrl-V` will also change to terminal mode.
`Insert` will change to terminal mode and replace the active command with the selected text.

