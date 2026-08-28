# Client-facing error sanitization

import logging
import re

from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

_logger = logging.getLogger(__name__)

SAFE_EXCEPTIONS = (UserError, AccessError, ValidationError, MissingError)

GENERIC_ERROR_MESSAGE = "Internal server error"

_TRACEBACK_MARKER = "Traceback (most recent call last)"
_EXCEPTION_LINE_RE = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Violation)\b"
)
_POSTGRES_DIAG_RE = re.compile(r"^(DETAIL|HINT|CONTEXT|LINE \d)", re.IGNORECASE)

_SCRUB_PATTERNS = (
    (re.compile(r'^\s*File "[^"]+", line \d+.*$', re.MULTILINE), ""),
    (re.compile(r"Traceback \(most recent call last\):"), ""),
    (
        re.compile(
            r"^[ \t]*(?:DETAIL|HINT|CONTEXT|QUERY|LINE \d+):.*$",
            re.IGNORECASE | re.MULTILINE,
        ),
        "",
    ),
    (re.compile(r"(?:/[^/\s]+)+/[^/\s]+\.py\b"), ""),
    (re.compile(r'"[^"]+\.py"'), ""),
    (re.compile(r",?\s*line\s+\d+"), ""),
    (re.compile(r"<class '[^']+'>"), ""),
    (re.compile(r"\b[Oo]bject at 0x[0-9a-fA-F]+"), "object"),
    (re.compile(r"\bat 0x[0-9a-fA-F]+"), ""),
    (re.compile(r"\b(?:odoo|mgn_mcp_server)\.[A-Za-z0-9_.]+:"), ""),
    (re.compile(r"\bpsycopg2(?:\.[A-Za-z0-9_.]+)?\b", re.IGNORECASE), ""),
)


def sanitize_exception(exc, log_context=None):
    if isinstance(exc, SAFE_EXCEPTIONS):
        return sanitize_message(_exc_message(exc))
    _logger.error(log_context or "Unhandled adv_mcp error", exc_info=exc)
    return GENERIC_ERROR_MESSAGE


def sanitize_message(text):
    if not text:
        return GENERIC_ERROR_MESSAGE

    text = _reduce_traceback(str(text))
    for pattern, replacement in _SCRUB_PATTERNS:
        text = pattern.sub(replacement, text)

    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text or GENERIC_ERROR_MESSAGE


def _exc_message(exc):
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], str):
        return args[0]
    return str(exc)


def _reduce_traceback(text):
    if _TRACEBACK_MARKER not in text:
        return text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    while lines and _POSTGRES_DIAG_RE.match(lines[-1]):
        lines.pop()
    if not lines:
        return GENERIC_ERROR_MESSAGE

    last_frame = max(
        (i for i, line in enumerate(lines) if line.startswith('File "')),
        default=-1,
    )
    tail = lines[last_frame + 1:]
    exc_idx = next(
        (i for i, line in enumerate(tail) if _EXCEPTION_LINE_RE.match(line)),
        len(tail) - 1,
    )
    final = "\n".join(tail[exc_idx:])
    final = re.split(r"\s+DETAIL:", final)[0].strip()
    if not re.search(r"[A-Za-z]", final):
        return GENERIC_ERROR_MESSAGE
    return final
