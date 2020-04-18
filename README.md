# Sublime GidTerm

Terminal that runs inside SublimeText. Very much alpha quality. Linux only.

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

`Shift-Ctrl-G` in an existing tab to open a tab with a shell.
If the existing tab contains a file, the shell will be in the same directory.

The tab starts in shell mode where control keys are sent to the terminal.

Use PgUp / PgDn to move to edit mode where control keys perform Sublime commands.

Use Insert or type a standard key to move back to shell mode.
