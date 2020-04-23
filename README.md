# Sublime GidTerm

Terminal that runs inside Sublime Text. Alpha quality. Linux only.

## Install

Clone this repo into your Sublime Text packages repository.

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

In terminal mode, change to browse mode using one of the following:

- Click above the active command line; 
- `Home`, `End`, `PageUp`, `PageDown` (including with `Shift` and `Ctrl` modifiers) to perform the usual Sublime Text navigation;
- `Shift-Ctrl-PageUp` and `Shift-Ctrl-PageDown` to select the previous or next command respectively.

In browse mode, change to terminal mode using one of the following:

- Type any printing character, including `Enter`, `Tab`, `Backspace`, and `Delete`;
- `Ctrl-V` to insert the clipboard into the command prompt;
- `Ctrl-Insert` to insert the clipboard into the command prompt with surrounding whitespace removed;
- `Shift-Insert` to delete the contents of the command prompt and replace with the clipboard with surrounding whitespace removed;
- `Shift-Delete` to delete the contents of the command prompt;

All of these, except `Ctrl-V`, perform the same action in both modes.
