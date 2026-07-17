# Map in a Box localisation

Map in a Box uses Python gettext.

Runtime translation files belong in:

```text
locale/<language>/LC_MESSAGES/mapinabox.po
locale/<language>/LC_MESSAGES/mapinabox.mo
```

English strings in the source code are the fallback.  Wrap user-facing strings
with `_()`, and use named placeholders for dynamic values:

```python
_("Looking up {place}...").format(place=name)
```

For spoken/status messages, add a translator comment immediately above the
string:

```python
# Translators: Spoken when the user reaches the end of the current street.
self.update_ui(_("End of {street}.").format(street=street), force=True)
```

Use `ngettext` for plurals and `pgettext` when a short word is ambiguous.
