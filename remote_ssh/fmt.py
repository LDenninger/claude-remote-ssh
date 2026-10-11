"""Output grammar shared by every tool result and injected message of the plugin.

A result is a first line `<tool> <host> · <facts>`, fixed `key: value` lines, and named blocks
`--- <name> ---` for multi-line content. An error is `<tool> <host> · error` followed by `reason:`
and `next:`. Text that came from a remote machine goes through `quote`, which prefixes every line
with `> `.
"""
from __future__ import annotations

from typing import Any

SEP = ' · '


def head(tool: str, host: str | None = None, *facts: Any) -> str:
    """Return the first line: tool word, optional host and facts joined by ` · `."""
    return SEP.join([f'{tool} {host}' if host else tool, *(str(fact) for fact in facts if fact not in (None, ''))])


def count(number: int, noun: str) -> str:
    """Return `number` and `noun`, the noun pluralised with `s` unless the number is 1."""
    return f'{number} {noun}' if number == 1 else f'{number} {noun}s'


def fields(*pairs: tuple[str, Any]) -> list[str]:
    """Return one `key: value` line per pair, skipping pairs whose value is None."""
    return [f'{key}: {value}' for key, value in pairs if value is not None]


def quote(text: str) -> str:
    """Prefix every line of `text` with `> `, an empty text yielding one bare `>`."""
    return '\n'.join(f'> {line}'.rstrip() for line in (text.splitlines() or ['']))


def block(title: str, text: str) -> str:
    """Return `--- <title> ---` above `text`, or an empty string when `text` is empty."""
    return f'--- {title} ---\n{text}' if text else ''


def table(header: list[str], rows: list[list[str]]) -> str:
    """Return a plain-text table with columns padded to the widest cell, one row per line."""
    widths = [max(len(row[col]) for row in [header, *rows]) for col in range(len(header))]
    return '\n'.join('  '.join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in [header, *rows])


def error(tool: str, host: str | None, reason: str, next_step: str, *extra: tuple[str, Any]) -> str:
    """Return an error result: `<tool> <host> · error`, `reason:`, extra fields, then `next:`."""
    return compose(head(tool, host, 'error'), *fields(('reason', reason), *extra, ('next', next_step)))


def compose(*parts: str) -> str:
    """Join the non-empty parts with newlines."""
    return '\n'.join(part for part in parts if part)
