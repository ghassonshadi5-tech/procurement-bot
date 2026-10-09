"""
Internationalization (i18n) Helper for Procurement Bot.

Supports Arabic (ar), English (en), and Russian (ru).

STATUS: PARTIAL ACTIVATION (IN PROGRESS)
=========================================
Activation is being rolled out in stages:
- Step 1 (DONE): UserMiddleware injects data["lang"] into every handler.
- Step 2 (DONE): This module exposes `t()` as a shorthand for get_translation.
- Step 3 (PENDING): Language-selection screen on first /start.
- Step 4 (PENDING): /start and /help use i18n.
- Step 5 (PENDING): Expand locale files with full coverage.

Currently all other user-facing text is still hard-coded in Arabic
throughout the codebase and will be migrated progressively.

IMPROVEMENTS (this revision):
- Added `t()` shorthand for get_translation().
- Caching: translations are loaded once per language and kept in memory.
- Fallback chain: requested lang → default lang → key.
- Structured logging with %s formatting and logger.exception.
- Exposes `get_supported_languages()`, `is_supported()`,
  `get_default_language()`, and `reload_translations()`.
"""
import json
import os
from typing import Any, Dict, List

from app.core.logger import get_logger

logger = get_logger(__name__)


# Path to locales directory
LOCALES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "locales")
)

# Whitelisted languages and default fallback
SUPPORTED_LANGUAGES: List[str] = ["ar", "en", "ru"]
DEFAULT_LANGUAGE: str = "ar"

# In-memory cache: {lang: {key: value}}
_TRANSLATIONS_CACHE: Dict[str, Dict[str, Any]] = {}


# ============================================================================
# Internal helpers
# ============================================================================
def _load_translations(lang: str) -> Dict[str, Any]:
    """
    Load a language's translations from disk (once) and cache them.

    Returns an empty dict if the file is missing or invalid.
    """
    if lang in _TRANSLATIONS_CACHE:
        return _TRANSLATIONS_CACHE[lang]

    file_path = os.path.join(LOCALES_DIR, f"{lang}.json")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error(
                "Translation file is not a JSON object | lang=%s | path=%s",
                lang,
                file_path,
            )
            data = {}
    except FileNotFoundError:
        logger.error(
            "Translation file not found | lang=%s | path=%s",
            lang,
            file_path,
        )
        data = {}
    except json.JSONDecodeError:
        logger.exception(
            "Translation file is invalid JSON | lang=%s | path=%s",
            lang,
            file_path,
        )
        data = {}
    except Exception:
        logger.exception(
            "Unexpected error loading translations | lang=%s | path=%s",
            lang,
            file_path,
        )
        data = {}

    _TRANSLATIONS_CACHE[lang] = data
    return data


def _normalize_language(lang: str) -> str:
    """Return a whitelisted language code, falling back to default."""
    if not lang or lang not in SUPPORTED_LANGUAGES:
        return DEFAULT_LANGUAGE
    return lang


# ============================================================================
# Public API
# ============================================================================
def get_translation(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """
    Get translated text by key and language.

    Args:
        key: The translation key (e.g., 'welcome').
        lang: Language code ('ar', 'en', 'ru'). Defaults to 'ar'.
        **kwargs: Variables to format into the string (e.g., name='Ali').

    Returns:
        Translated string, or the key itself if no translation is found.
    """
    normalized_lang = _normalize_language(lang)

    translations = _load_translations(normalized_lang)

    # Fallback to the default language if the key is missing in the requested one
    text = translations.get(key)
    if text is None and normalized_lang != DEFAULT_LANGUAGE:
        text = _load_translations(DEFAULT_LANGUAGE).get(key)

    if text is None:
        # Return the key itself as a last resort (visible debugging hint)
        return key

    # Format variables if any (e.g., {name})
    if kwargs:
        try:
            text = text.format(**kwargs)
        except KeyError:
            logger.warning(
                "Missing format variable | key=%s | lang=%s | provided=%s",
                key,
                normalized_lang,
                list(kwargs.keys()),
            )
        except Exception:
            logger.exception(
                "Failed to format translation | key=%s | lang=%s",
                key,
                normalized_lang,
            )

    return text


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """
    Shorthand for get_translation().

    Usage in handlers:
        from app.core.i18n import t
        text = t("welcome", lang=lang, name=user.full_name)
    """
    return get_translation(key, lang=lang, **kwargs)


def get_supported_languages() -> List[str]:
    """Return the list of whitelisted language codes."""
    return list(SUPPORTED_LANGUAGES)


def get_default_language() -> str:
    """Return the default language code."""
    return DEFAULT_LANGUAGE


def is_supported(lang: str) -> bool:
    """Check whether a language code is whitelisted."""
    return bool(lang) and lang in SUPPORTED_LANGUAGES


def reload_translations() -> None:
    """
    Clear the in-memory translation cache.

    Intended for development and testing only. Production code should
    never need to call this at runtime.
    """
    _TRANSLATIONS_CACHE.clear()
    logger.info("Translation cache cleared.")