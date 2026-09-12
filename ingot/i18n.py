"""Translation setup.

The source language is English. Catalogues are looked up in the system locale
directory, or in build-i18n/locale when running from a source checkout after
compiling them. INGOT_LOCALEDIR overrides both.

The language follows the standard environment variables (LANGUAGE, LC_ALL,
LC_MESSAGES, LANG); no system locale has to be generated for the interface
text to be translated.
"""

from __future__ import annotations

import gettext
import os
from pathlib import Path

DOMAIN = "ingot"


def _locale_dir() -> str | None:
    override = os.environ.get("INGOT_LOCALEDIR")
    if override:
        return override
    checkout = Path(__file__).resolve().parent.parent / "build-i18n" / "locale"
    if checkout.is_dir():
        return str(checkout)
    return None  # gettext's default: <prefix>/share/locale


_translation = gettext.translation(DOMAIN, localedir=_locale_dir(), fallback=True)

_ = _translation.gettext
ngettext = _translation.ngettext


def N_(message: str) -> str:
    """Mark a string for extraction without translating it yet."""
    return message
