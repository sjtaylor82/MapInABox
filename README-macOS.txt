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
