# Copyright (c) 2025-2026 Tim Molteno (tim@elec.ac.nz)
"""The ordered ``--flag`` grammar.

Flagging operations and their parameters are given as one ordered list, so the
order the operations run in is the order they are written:

    --flag "save:before, uv-above 2000, nan, clip 0 100, save:after"

Entries are ``verb [args...]`` separated by top-level commas.  Commas inside
square brackets are not separators, so a spectral-window rule file and its
ranges stay intact.  Quoting is honoured for paths containing spaces.

``barber`` is deliberately **not** a verb here: it does not write ``FLAG`` (see
:mod:`skarabina.barber`), so it is a read-only diagnostic with no place in an
ordering of flagging operations.  It remains a separate CLI option, and
``--flag barber`` is rejected rather than ignored.
"""

import pathlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import click
import yaml

from skarabina.rflag import RFlagParams
from skarabina.tfcrop import TFCropParams

# Canonical order used by the 0.8.x flagger, kept as the migration reference in
# the documentation.  The new interface does NOT consult it: the list is the
# run, so there is no unlisted-but-enabled operation to place.
CANONICAL_ORDER: Tuple[str, ...] = (
    "autos",
    "uv-above",
    "nan",
    "clip",
    "tfcrop",
    "rflag",
    "spectral-window",
)

# Accepted spellings of each verb.  ``uv-above`` is the documented form; the
# others are accepted so a shell or a hurried typist cannot silently change the
# meaning of a run.
VERB_ALIASES = {
    "autos": "autos",
    "uv-above": "uv-above",
    "uv_above": "uv-above",
    "uvabove": "uv-above",
    "nan": "nan",
    "clip": "clip",
    "spectral-window": "spectral-window",
    "spectral_window": "spectral-window",
    "spectralwindow": "spectral-window",
    "tfcrop": "tfcrop",
    "rflag": "rflag",
}

# Markers take a name rather than a value, hence the colon form.
MARKER_VERBS = ("save", "restore")

# Verbs that take no arguments.
NULLARY = ("autos", "nan")

# Operations excluded on purpose, with the reason to show the user.
REJECTED = {
    "barber": (
        "barber is not a flagging operation: it does not write FLAG, it is a"
        " read-only diagnostic, so it has no place in the flagging order."
        " Use the separate --barber option"
    ),
}


class FlagOrderError(click.BadParameter):
    """A ``--flag`` entry could not be understood.

    A :class:`click.BadParameter`, so Click renders it as a usage error with the
    offending option named, rather than a traceback.
    """

    def __init__(self, message: str):
        super().__init__(message, param_hint="--flag")


@dataclass(frozen=True)
class FlagOp:
    """One parsed ``--flag`` entry."""

    verb: str
    args: Tuple[str, ...] = ()
    #: the entry exactly as written, for error messages and console output
    entry: str = field(default="", compare=False)

    @property
    def is_marker(self) -> bool:
        return self.verb in MARKER_VERBS

    @property
    def name(self) -> Optional[str]:
        """The version name of a ``save``/``restore`` marker."""
        if not self.is_marker:
            return None
        return self.args[0] if self.args else None

    def describe(self) -> str:
        """How this operation is announced in the console log."""
        if self.is_marker:
            return f"{self.verb}:{self.name}"
        if not self.args:
            return self.verb
        return f"{self.verb} {' '.join(self.args)}"


def split_top_level(text: str) -> List[str]:
    """Split on commas that are not inside brackets or quotes.

    Keeps ``[850, 900]`` and ``'my rules.yml'`` in one piece.
    """
    parts, current = [], []
    depth = 0
    quote = None
    escaped = False
    for ch in text:
        if escaped:
            # A backslash-escaped bracket must not count towards the depth:
            # stimela escapes ``[`` and ``]`` when passing a parameter to a
            # container, so a comma inside a bracketed entry would otherwise be
            # taken for a top-level separator and split the entry in two.
            escaped = False
        elif ch == "\\":
            escaped = True
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _tokenize(entry: str) -> List[str]:
    """Split an entry into whitespace-separated tokens, honouring quotes.

    A backslash escapes the next character.  Stimela escapes brackets when it
    passes a parameter to a container, and without this the escaped bracket
    survives into a parameter name and the entry is rejected with a confusing
    message.
    """
    tokens, current, quote, escaped = [], [], None, False
    for ch in entry:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch == "\\":
            escaped = True
        elif ch in "'\"":
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if quote:
        raise FlagOrderError(f"unbalanced quote in entry {entry!r}")
    if current:
        tokens.append("".join(current))
    return tokens


def _parse_name(verb: str, raw: str, entry: str) -> str:
    """Validate the version name of a ``save``/``restore`` marker."""
    if not raw:
        raise FlagOrderError(
            f"entry {entry!r}: save/restore needs a version name,"
            f" e.g. '{verb}:before'"
        )
    if "/" in raw or "\\" in raw:
        raise FlagOrderError(
            f"entry {entry!r}: a version name cannot contain a path separator"
        )
    return raw


def parse_entry(entry: str) -> FlagOp:
    """Parse one ``--flag`` entry into a :class:`FlagOp`."""
    tokens = _tokenize(entry.strip())
    if not tokens:
        raise FlagOrderError("empty entry in --flag (a stray comma?)")
    head, rest = tokens[0], tuple(tokens[1:])

    # A save:/restore: marker is written as one token.
    if ":" in head:
        verb, _, name = head.partition(":")
        verb = verb.strip().lower()
        if verb not in MARKER_VERBS:
            raise FlagOrderError(
                f"entry {entry!r}: unknown marker '{verb}:'."
                f" Valid markers are {', '.join(v + ':' for v in MARKER_VERBS)}"
            )
        if rest:
            raise FlagOrderError(
                f"entry {entry!r}: {verb}: takes no further arguments"
            )
        return FlagOp(verb, (_parse_name(verb, name, entry),), entry)

    verb = VERB_ALIASES.get(head.strip().lower())
    if verb is None:
        if head.strip().lower() in REJECTED:
            raise FlagOrderError(
                f"entry {entry!r}: {REJECTED[head.strip().lower()]}"
            )
        valid = ", ".join(CANONICAL_ORDER)
        raise FlagOrderError(
            f"entry {entry!r}: unknown verb '{head}'."
            f" Valid verbs are {valid}, plus save:NAME and restore:NAME"
        )

    if verb in NULLARY:
        if rest:
            raise FlagOrderError(
                f"entry {entry!r}: {verb} takes no arguments"
            )
        return FlagOp(verb, (), entry)

    if verb == "clip":
        if len(rest) != 2:
            raise FlagOrderError(
                f"entry {entry!r}: clip needs two values, e.g. 'clip 0 100'"
            )
        for value in rest:
            try:
                float(value)
            except ValueError:
                raise FlagOrderError(
                    f"entry {entry!r}: clip values must be numbers"
                ) from None
        return FlagOp(verb, rest, entry)

    if verb == "uv-above":
        if len(rest) != 1:
            raise FlagOrderError(
                f"entry {entry!r}: uv-above needs one value in metres,"
                " e.g. 'uv-above 4000'"
            )
        try:
            float(rest[0])
        except ValueError:
            raise FlagOrderError(
                f"entry {entry!r}: uv-above value must be a number"
            ) from None
        return FlagOp(verb, rest, entry)

    if verb == "tfcrop":
        return FlagOp(
            verb, _parse_autofit_args(rest, entry, TFCropParams), entry
        )

    if verb == "rflag":
        return FlagOp(
            verb, _parse_autofit_args(rest, entry, RFlagParams), entry
        )

    if verb == "spectral-window":
        if len(rest) != 1:
            raise FlagOrderError(
                f"entry {entry!r}: spectral-window needs one rule file,"
                " e.g. 'spectral-window rules.yml'"
            )
        return FlagOp(verb, rest, entry)

    raise FlagOrderError(f"entry {entry!r}: unhandled verb '{verb}'")


def _parse_autofit_args(rest, entry, params_class) -> Tuple[str, ...]:
    """Parameters for an auto-flagging verb, as ``key=value`` pairs.

    Written after the verb, in brackets, or with colons, all equivalent::

        tfcrop timecutoff=5 freqcutoff=2.5
        tfcrop [timecutoff=5, freqcutoff=2.5]
        tfcrop timecutoff: 5

    Keyword form rather than positional because the verb has nine parameters
    whose names are the ones CASA's ``flagdata`` uses, so a recipe reads the
    same way it would there and only the parameters being changed need naming.
    The parameter class validates them, so a typo is an error rather than a
    silently ignored setting.

    The colon form exists because the parameters usually arrive inside a YAML
    list, one entry per operation, and a mapping is the natural way to write
    several of them there::

        flag:
          - tfcrop timefit: line usewindowstats: both

    A colon is only read as a separator when what precedes it is a real
    parameter name.  ``save:`` and ``restore:`` are the grammar's other colon
    syntax and a rule file path may contain one, so a colon is not treated as a
    separator in general.
    """
    tokens = [t.strip() for t in rest]
    if tokens and tokens[0].startswith("[") and tokens[-1].endswith("]"):
        tokens[0] = tokens[0][1:]
        tokens[-1] = tokens[-1][:-1]
    pieces = []
    for token in tokens:
        # Accept commas as well as spaces between parameters, so a bracketed
        # list and a bare run of pairs mean the same thing.
        for piece in _unescape_brackets(token).split(","):
            piece = piece.strip()
            if piece:
                pieces.append(piece)

    named: List[str] = []
    index = 0
    while index < len(pieces):
        piece = pieces[index]
        index += 1
        head, sep, tail = piece.partition("=")
        if not sep:
            colon_head, colon_sep, colon_tail = piece.partition(":")
            if colon_sep and colon_head.strip() in params_class.DEFAULTS:
                head, sep, tail = colon_head, colon_sep, colon_tail
        if not sep:
            raise FlagOrderError(
                f"entry {entry!r}: tfcrop parameters are given as key=value, so"
                f" {piece!r} is missing its separator. For example"
                " 'tfcrop timecutoff=5 freqcutoff=2.5',"
                " 'tfcrop [timecutoff=5, freqcutoff=2.5]', or"
                " 'tfcrop timecutoff: 5'"
            )
        name, value = head.strip(), tail.strip()
        if not value:
            # 'timefit: line' arrives as the two pieces 'timefit:' and 'line';
            # the next piece is the value, and is consumed here so it is not
            # read again as a parameter of its own.
            if index == len(pieces):
                raise FlagOrderError(
                    f"entry {entry!r}: tfcrop parameter {name!r} has no value"
                )
            value = pieces[index]
            index += 1
        named.append(f"{name}={value}")
    try:
        # Constructed for its validation side effect; the values are parsed
        # again at run time, so the parsed form stays a plain tuple of strings.
        params_class(**dict(_coerce_parameters(named)))
    except ValueError as exc:
        raise FlagOrderError(f"entry {entry!r}: {exc}") from None
    return tuple(named)


def _unescape_brackets(text: str) -> str:
    """``\\[`` -> ``[`` and ``\\]`` -> ``]``.

    Stimela escapes brackets on the way to a container.  They are decoration in
    this grammar -- the parameters are the ``key=value`` pairs -- so the
    backslashes are dropped rather than being treated as part of a name.
    """
    out, escaped = [], False
    for ch in text:
        if escaped:
            out.append(ch if ch in "[](){}" else "\\" + ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    if escaped:
        out.append("\\")
    return "".join(out)


def _coerce_parameters(named):
    """``key=value`` strings to typed keyword arguments for a parameter class."""
    coerced = {}
    for token in named:
        key, _, value = token.partition("=")
        coerced[key.strip()] = _literal(value.strip())
    return coerced


def _literal(text: str):
    """A parameter value as the type it looks like.

    Only the types CASA's tfcrop parameters actually take: numbers, one of a
    few names, and booleans.  Anything else stays a string, which
    the parameter class then rejects by name.
    """
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _spec_entries(spec: str) -> List[str]:
    """Entries in one ``--flag`` value: a comma run, or a YAML/JSON list."""
    text = spec.strip()
    if text.startswith("["):
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise FlagOrderError(f"could not parse {spec!r} as a list: {exc}") from None
        if not isinstance(loaded, list):
            raise FlagOrderError(f"{spec!r} is not a list of entries")
        return [str(item) for item in loaded]
    return split_top_level(spec)


def parse(specs) -> List[FlagOp]:
    """Parse every ``--flag``/``--flag-file`` value into operations, in order.

    Each value may be a comma-separated run of entries or a YAML/JSON list of
    them; occurrences are concatenated in the order given.
    """
    ops: List[FlagOp] = []
    for spec in specs or ():
        for entry in _spec_entries(spec):
            ops.append(parse_entry(entry))
    return ops


def load_file(path) -> List[str]:
    """Entries from a ``--flag-file``.

    Accepts a YAML list of entries, or plain text with one entry per line and
    ``#`` comments.  Returns the entry strings, unparsed.
    """
    text = pathlib.Path(path).read_text()
    stripped = text.lstrip()
    # A YAML mapping is a plausible mistake (keys mapping to switches) and would
    # otherwise be read as one long, meaningless entry per line.
    first = next(
        (ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")),
        "",
    )
    if ": " in first or first.rstrip().endswith(":"):
        raise FlagOrderError(
            f"{path}: expected a list of entries or one entry per line, not a"
            " mapping"
        )
    if stripped.startswith("[") or stripped.startswith("- ") or stripped.startswith("---"):
        loaded = yaml.safe_load(text)
        if loaded is None:
            return []
        if not isinstance(loaded, list):
            raise FlagOrderError(
                f"{path}: expected a list of entries or one entry per line"
            )
        return [str(item) for item in loaded]
    entries = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def run(ms, ops, log=print, flush=True):
    """Run parsed operations in order against a :class:`DaskMS`.

    Returns the number of operations run.  Every step's statistics are queued
    (``ms.defer_reports``) and computed together in the next pass over the
    data, so a sequence of N operations reads the data once rather than N
    times: rflag/tfcrop compute the reports queued before them in their own
    pass, and the rest are computed at the end -- here when ``flush`` is set,
    or, with ``flush=False``, by the caller's next pass
    (``ms.materialise_flags()`` or ``ms.flush_reports()``), which is how the
    CLI folds them into the pass that materialises the flags.
    """
    ms.defer_reports = True
    for op in ops:
        log(op.describe())
        if op.verb == "save":
            ms.save_flag_version(op.name)
        elif op.verb == "restore":
            ms.restore_flag_version(op.name)
        elif op.verb == "autos":
            ms.flag_autocorrelations()
        elif op.verb == "uv-above":
            ms.flag_uv_above(float(op.args[0]))
        elif op.verb == "nan":
            ms.flag_data({"NAN": True})
        elif op.verb == "clip":
            ms.flag_data({"CLIP": (float(op.args[0]), float(op.args[1]))})
        elif op.verb == "tfcrop":
            ms.flag_tfcrop(TFCropParams(**_coerce_parameters(op.args)))
        elif op.verb == "rflag":
            ms.flag_rflag(RFlagParams(**_coerce_parameters(op.args)))
        elif op.verb == "spectral-window":
            ms.flag_spectral_window(op.args[0])
        else:  # pragma: no cover - parse() rejects anything else
            raise FlagOrderError(f"unhandled verb {op.verb!r}")
    if flush:
        ms.flush_reports()
    return len(ops)
