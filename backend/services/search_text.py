"""Literal Unicode matching for existing SQLite-backed library searches."""
import sqlite3


def literal_contains(connection: sqlite3.Connection, column: str, term: str) -> tuple[str, str]:
    """Return a predicate for a trusted column expression and a bound query.

    SQLite lower/NOCASE only fold ASCII. Keep the existing substring semantics,
    including literal percent/underscore, while matching Unicode case variants.
    The caller supplies a static SQL expression, never user-authored SQL.
    """
    connection.create_function('keivotos_casefold', 1, lambda value: (value or '').casefold(), deterministic=True)
    return f'instr(keivotos_casefold({column}), ?)>0', term.casefold()
