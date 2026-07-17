"""Minimal gettext helpers for Map in a Box localisation.

English source strings are the fallback.  Translations live under
locale/<language>/LC_MESSAGES/mapinabox.mo when available.
"""

from __future__ import annotations

import gettext
import locale
import os
import sys

DOMAIN = "mapinabox"
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
LOCALE_DIR = os.path.join(BASE_DIR, "locale")

_translation: gettext.NullTranslations = gettext.translation(
    DOMAIN,
    localedir=LOCALE_DIR,
    fallback=True,
)


def set_language(language: str | None = None) -> str:
    """Load translations for a language code, falling back to English."""
    global _translation
    languages = None
    if language:
        languages = [language]
    _translation = gettext.translation(
        DOMAIN,
        localedir=LOCALE_DIR,
        languages=languages,
        fallback=True,
    )
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    return language or ""


def _(message: str) -> str:
    return _translation.gettext(message)


def pgettext(context: str, message: str) -> str:
    return _translation.pgettext(context, message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


def npgettext(context: str, singular: str, plural: str, n: int) -> str:
    return _translation.npgettext(context, singular, plural, n)
