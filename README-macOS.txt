Map in a Box — macOS First Launch
==================================

macOS will block the app on first launch because it is not signed with
an Apple developer certificate. Follow these steps to open it:

Step 1 — Open Terminal
  Terminal is in Applications > Utilities, or search for it with Spotlight.

Step 2 — Run the install script
  Type the following command and press Enter:

    bash ~/Downloads/install-macos.sh

  Or drag the install-macos.sh file into the Terminal window after typing
  "bash " (with a space), then press Enter.

Step 3 — Done
  The script removes the restriction, copies Map in a Box to your
  Applications folder, and opens it. You will not need to do this again.


If you prefer to do it manually, run this command in Terminal:

  xattr -rd com.apple.quarantine /path/to/MapInABox.app

Then drag MapInABox.app to your Applications folder and open it normally.


For help and support visit:
  https://github.com/sjtaylor82/MapInABox


macOS Keyboard Notes
====================

Shortcuts written as Ctrl use Command on macOS. Shortcuts written as Alt use
Option. On the tested Mac, use Control+F11 when bare F11 does not reach Map in
a Box. Bare F12 opens Tools normally. Physical Control is not a general
substitute for Ctrl in the documentation; a documented Ctrl+F12 action uses
Command+F12 on macOS.


Logs
====

The application log is stored at:
  ~/Library/Application Support/MapInABox/miab.log
