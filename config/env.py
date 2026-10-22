from __future__ import annotations

class DotenvParseError(ValueError):
    """Raised when a .env line cannot be parsed. Message MUST include only the line number."""


def _find_unquoted_char(s: str, target: str) -> int | None:
    """Return index of first target char in s that is not inside single/double quotes; otherwise None."""
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == target and not in_single and not in_double:
            return i
    return None


def _strip_unquoted_comment(s: str) -> str:
    """Strip inline comment that starts with # outside of quotes."""
    idx = _find_unquoted_char(s, "#")
    return s[:idx].rstrip() if idx is not None else s.rstrip()


def parse_dotenv(content: str) -> dict[str, str]:
    """
    Parse minimal .env content into a key/value mapping.
    - Strips surrounding whitespace around keys and values
    - Supports single/double quoted values containing '=' and '#'
    - Treats ' #' outside quotes as a line comment
    - Allows empty values
    - Raises DotenvParseError naming the 1-based line number on malformed lines
    """
    result: dict[str, str] = {}
    for line_no, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Find the first '=' that is not inside quotes
        eq_idx = _find_unquoted_char(line, "=")
        if eq_idx is None:
            raise DotenvParseError(f"Malformed .env line {line_no}")
        key = line[:eq_idx].strip()
        if not key:
            raise DotenvParseError(f"Malformed .env line {line_no}")
        value_part = line[eq_idx + 1 :].strip()
        # Strip inline comment outside quotes
        value_visible = _strip_unquoted_comment(value_part)
        # Handle surrounding quotes if present
        if len(value_visible) >= 2 and (
            (value_visible[0] == value_visible[-1] == "'")
            or (value_visible[0] == value_visible[-1] == '"')
        ):
            value = value_visible[1:-1]
        else:
            # Validate quotes are balanced after comment removal
            in_single = False
            in_double = False
            for ch in value_visible:
                if ch == "'" and not in_double:
                    in_single = not in_single
                elif ch == '"' and not in_single:
                    in_double = not in_double
            if in_single or in_double:
                raise DotenvParseError(f"Malformed .env line {line_no}")
            value = value_visible
        result[key] = value
    return result

