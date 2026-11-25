liblouis 3.39.0 for Map in a Box
================================

Contracted (UEB grade 2) braille on the Dot Pad and Monarch, called from
braille.py through ctypes.

Source:   https://github.com/liblouis/liblouis/releases/tag/v3.39.0
          liblouis-3.39.0-win64.zip (bin/liblouis.dll, share/liblouis/tables)
SHA-256 of liblouis.dll:
          0f1194748deae4d2fc23888ae0787a5870b29a5a1095ab919b2a851ada020763
Licence:  LGPL-2.1-or-later (COPYING.LESSER), library and tables alike.

tables/ holds only en-ueb-g2.ctb and the eight tables it includes. To
update, replace liblouis.dll and these tables from a newer release and run
test_dotpad.py.
