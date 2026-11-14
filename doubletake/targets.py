"""Targets: the arguments that identify what an effect acts on (customer + amount, recipient + subject).

Free text (notes, bodies, reasons) is not part of the target, so rewording a retry does not make it a
new effect. Fields named `amount` are compared in cents; a field named `to` is compared case-insensitively.
"""

from collections.abc import Callable


def is_free_text(v) -> bool:
    """A string containing whitespace or longer than 40 characters."""
    return isinstance(v, str) and (any(c.isspace() for c in v.strip()) or len(v) > 40)


def target_of(args: dict, fields: tuple[str, ...] | None = None) -> tuple:
    """Normalized (field, value) pairs. Without `fields`: every argument except free-text strings."""
    if fields is None:
        fields = tuple(sorted(k for k, v in args.items() if not is_free_text(v)))
    out = []
    for f in fields:
        v = args.get(f)
        if f == "amount":
            try:
                v = round(float(v) * 100)  # cents
            except (TypeError, ValueError):
                v = str(v)
        elif f == "to":
            v = str(v or "").strip().lower()
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
        else:
            v = str(v if v is not None else "").strip()
        out.append((f, v))
    return tuple(out)


def generic_description(tool: str, target: tuple) -> str:
    return f"{tool}(" + ", ".join(f"{k}={v}" for k, v in target) + ")" if target else f"{tool}()"


Describer = Callable[[str, tuple], str]
