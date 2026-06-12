"""Top-level CLI dispatch: `live <verb> ...`."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from . import verbs
from .config import ConfigError
from .timeutil import parse_age, parse_time

_NAME_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")
_GEOMETRY_RE = re.compile(r"^(\d{1,5})x(\d{1,5})$")


def _parse_geometry(value: str) -> tuple[int, int]:
    """`COLSxROWS` (e.g. `200x24`) -> `(cols, rows)`, each 1-65535."""
    m = _GEOMETRY_RE.match(value)
    if m:
        cols, rows = int(m.group(1)), int(m.group(2))
        if 0 < cols <= 65535 and 0 < rows <= 65535:
            return cols, rows
    raise argparse.ArgumentTypeError(f"expected COLSxROWS, e.g. 200x24 (got {value!r})")


def _parse_name(value: str) -> str:
    """Names are shell-safe: letters, digits, `.`, `_`, `-`; no leading `-`."""
    if _NAME_RE.match(value):
        return value
    raise argparse.ArgumentTypeError(
        f"expected letters, digits, '.', '_', or '-', "
        f"not starting with '-' (got {value!r})"
    )


def _parse_cwd(value: str) -> Path:
    """Expand `~` and resolve to an absolute path; existence is not required
    (scope filtering against a deleted directory is still meaningful). An
    empty value is rejected: `Path("")` resolves to the invoking directory,
    silently masking an unset shell variable."""
    if not value:
        raise argparse.ArgumentTypeError("expected a directory path (got '')")
    return Path(value).expanduser().resolve()


def _count_or_cursor(prefix: str):
    """Build a parser for `N` (count) or `<prefix>N` (cursor) on `-n` / `-c`.

    `tail` uses `+N` (lines `n >= N`, Unix); `head` uses `-N` (drop last N,
    GNU). The opposite sign is accepted but treated as a plain count.
    """

    def parse(value: str) -> tuple[str, int]:
        if value.startswith(prefix):
            rest = value[1:]
            if rest.isdigit():
                return ("cursor", int(rest))
        elif value.startswith(("+", "-")) and value[1:].isdigit():
            return ("count", int(value[1:]))
        elif value.isdigit():
            return ("count", int(value))
        raise argparse.ArgumentTypeError(f"expected N or {prefix}N (got {value!r})")

    return parse


def _add_cwd_arg(p, help_text: str) -> None:
    """Add `-C/--cwd` to a parser or argument group."""
    p.add_argument(
        "-C",
        "--cwd",
        type=_parse_cwd,
        default=None,
        metavar="PATH",
        help=help_text,
    )


def _add_scope_flags(p: argparse.ArgumentParser) -> None:
    """Session scope: `-C` moves it to another directory, `-g` lifts the filter."""
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_",
        help="Global scope.",
    )
    _add_cwd_arg(g, "Directory scope (default: current directory).")


class _Formatter(argparse.HelpFormatter):
    """Render `REMAINDER` positionals using their metavar instead of `...`."""

    def _format_args(self, action, default_metavar):
        if action.nargs == argparse.REMAINDER:
            return "%s ..." % self._metavar_formatter(action, default_metavar)(1)
        return super()._format_args(action, default_metavar)


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live",
        description="Live stream command line output.",
        formatter_class=_Formatter,
        add_help=True,
    )
    p.add_argument("--version", action="version", version=f"live {__version__}")
    sub = p.add_subparsers(dest="verb", metavar="<verb>")

    # run
    run_p = sub.add_parser(
        "run",
        help="Run <cmd> under a PTY; record.",
        description="Run a command under a PTY and record its output.",
        formatter_class=_Formatter,
    )
    run_p.add_argument(
        "-n",
        "--name",
        type=_parse_name,
        default=None,
        help="Session name (letters, digits, '.', '_', '-').",
    )
    run_p.add_argument(
        "-d",
        "--detach",
        action="store_true",
        help="Detach: run in the background, print session id.",
    )
    _add_cwd_arg(run_p, "Run <cmd> in PATH; scope session.")
    run_p.add_argument(
        "--geometry",
        type=_parse_geometry,
        default=None,
        metavar="COLSxROWS",
        help="PTY size (default: the terminal's size, else 80x24).",
    )
    run_p.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        metavar="cmd",
        help="Command to run; `--` wraps subsequent arguments.",
    )
    run_p.set_defaults(func=verbs.cmd_run)

    # ls
    ls_p = sub.add_parser(
        "ls",
        help="List sessions.",
        description="List recorded sessions.",
        formatter_class=_Formatter,
    )
    ls_p.add_argument("-a", "--all", action="store_true", help="Include exited.")
    _add_scope_flags(ls_p)
    ls_p.add_argument("--json", action="store_true", help="Emit NDJSON.")
    ls_p.add_argument(
        "selector", nargs="?", default=None, help="NAME or UUID-prefix filter."
    )
    ls_p.set_defaults(func=verbs.cmd_ls)

    # cat
    cat_p = sub.add_parser(
        "cat",
        help="Concatenate session.",
        description="Display a session's full output.",
        formatter_class=_Formatter,
    )
    cat_p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    _add_scope_flags(cat_p)
    ag = cat_p.add_mutually_exclusive_group()
    ag.add_argument(
        "--strip-ansi",
        action="store_true",
        dest="strip_ansi",
        help="Strip ANSI.",
    )
    ag.add_argument("--raw", action="store_true", dest="raw", help="Keep ANSI.")
    cat_p.add_argument("selector", help="NAME or UUID-prefix.")
    cat_p.set_defaults(func=verbs.cmd_cat)

    # head
    head_p = sub.add_parser(
        "head",
        help="Head session.",
        description="Display the first part of a session.",
        formatter_class=_Formatter,
    )
    head_p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    _add_scope_flags(head_p)
    ag = head_p.add_mutually_exclusive_group()
    ag.add_argument(
        "--strip-ansi",
        action="store_true",
        dest="strip_ansi",
        help="Strip ANSI.",
    )
    ag.add_argument("--raw", action="store_true", dest="raw", help="Keep ANSI.")
    mode = head_p.add_mutually_exclusive_group()
    mode.add_argument(
        "-n",
        "--lines",
        type=_count_or_cursor("-"),
        default=None,
        help="First N lines (default 10); -N drops last N.",
    )
    mode.add_argument(
        "-c",
        "--bytes",
        dest="bytes_",
        metavar="BYTES",
        type=_count_or_cursor("-"),
        default=None,
        help="First K bytes; -K drops last K.",
    )
    mode.add_argument(
        "-t",
        "--time",
        type=parse_time,
        default=None,
        help="Lines with idx t <= T: epoch, duration (30m), or ISO datetime.",
    )
    head_p.add_argument("selector", help="NAME or UUID-prefix.")
    head_p.set_defaults(func=verbs.cmd_head)

    # tail
    tail_p = sub.add_parser(
        "tail",
        help="Tail session.",
        description="Display the last part of a session.",
        formatter_class=_Formatter,
    )
    tail_p.add_argument(
        "-f", "--follow", action="store_true", help="Follow until exit."
    )
    tail_p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    _add_scope_flags(tail_p)
    ag = tail_p.add_mutually_exclusive_group()
    ag.add_argument(
        "--strip-ansi",
        action="store_true",
        dest="strip_ansi",
        help="Strip ANSI.",
    )
    ag.add_argument("--raw", action="store_true", dest="raw", help="Keep ANSI.")
    mode = tail_p.add_mutually_exclusive_group()
    mode.add_argument(
        "-n",
        "--lines",
        type=_count_or_cursor("+"),
        default=None,
        help="Last N lines (default 10); +N for lines n >= N.",
    )
    mode.add_argument(
        "-c",
        "--bytes",
        dest="bytes_",
        metavar="BYTES",
        type=_count_or_cursor("+"),
        default=None,
        help="Last K bytes; +K for bytes from position K.",
    )
    mode.add_argument(
        "-t",
        "--time",
        type=parse_time,
        default=None,
        help="Lines with idx t > T: epoch, duration (30m), or ISO datetime.",
    )
    tail_p.add_argument("selector", help="NAME or UUID-prefix.")
    tail_p.set_defaults(func=verbs.cmd_tail)

    # less
    less_p = sub.add_parser(
        "less",
        help="Page session.",
        description="Page session interactively.",
        formatter_class=_Formatter,
    )
    _add_scope_flags(less_p)
    ag = less_p.add_mutually_exclusive_group()
    ag.add_argument(
        "--strip-ansi",
        action="store_true",
        dest="strip_ansi",
        help="Strip ANSI.",
    )
    ag.add_argument("--raw", action="store_true", dest="raw", help="Keep ANSI.")
    less_p.add_argument("selector", help="NAME or UUID-prefix.")
    less_p.set_defaults(func=verbs.cmd_less)

    # stop
    stop_p = sub.add_parser(
        "stop",
        help="Stop running sessions.",
        description="Stop running sessions.",
        formatter_class=_Formatter,
    )
    _add_scope_flags(stop_p)
    stop_p.add_argument(
        "--all",
        action="store_true",
        dest="all_",
        help="Stop all running sessions.",
    )
    stop_p.add_argument("selectors", nargs="*", help="NAME(s) or UUID-prefix(es).")
    stop_p.set_defaults(func=verbs.cmd_stop)

    # rm
    rm_p = sub.add_parser(
        "rm",
        help="Delete sessions.",
        description="Remove recorded sessions.",
        formatter_class=_Formatter,
    )
    rm_p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="SIGTERM live runs; ignore missing.",
    )
    _add_scope_flags(rm_p)
    rm_p.add_argument(
        "--all",
        action="store_true",
        dest="all_",
        help="Delete all sessions.",
    )
    rm_p.add_argument(
        "--exited",
        action="store_true",
        help="Delete exited sessions.",
    )
    rm_p.add_argument(
        "--untitled",
        action="store_true",
        help="Delete unnamed sessions.",
    )
    rm_p.add_argument(
        "--older-than",
        type=parse_age,
        default=None,
        dest="older_than",
        metavar="AGE",
        help="Delete sessions exited before AGE: duration (7d, 12h, 30m, 60s) or ISO datetime.",
    )
    rm_p.add_argument("selectors", nargs="*", help="NAME(s) or UUID-prefix(es).")
    rm_p.set_defaults(func=verbs.cmd_rm)

    # completion
    comp_p = sub.add_parser(
        "completion",
        help="Print completion candidates.",
        description="Print completion candidates, one per line "
        "(plumbing for the shell completion scripts).",
        formatter_class=_Formatter,
    )
    comp_sub = comp_p.add_subparsers(dest="what", metavar="<what>", required=True)
    sel_p = comp_sub.add_parser(
        "selectors",
        help="Session names and ids.",
        description="Print session names and ids.",
        formatter_class=_Formatter,
    )
    sel_p.add_argument("-a", "--all", action="store_true", help="Include exited.")
    _add_scope_flags(sel_p)
    sel_p.set_defaults(func=verbs.cmd_completion_selectors)
    cwds_p = comp_sub.add_parser(
        "cwds",
        help="Session working directories.",
        description="Print the distinct cwds of all sessions.",
        formatter_class=_Formatter,
    )
    cwds_p.set_defaults(func=verbs.cmd_completion_cwds)

    # completion-script
    script_p = sub.add_parser(
        "completion-script",
        help="Print shell completion script.",
        description="Print shell completion script.",
        formatter_class=_Formatter,
    )
    script_p.add_argument(
        "shell", choices=["bash", "zsh", "fish"], help="Target shell."
    )
    script_p.set_defaults(func=verbs.cmd_completion_script)

    # update-shell
    up_p = sub.add_parser(
        "update-shell",
        help="Install shell completions.",
        description="Install shell completions.",
        formatter_class=_Formatter,
    )
    up_p.add_argument(
        "shell",
        nargs="?",
        choices=["bash", "zsh", "fish"],
        default=None,
        help="Target shell (default: $SHELL).",
    )
    up_p.set_defaults(func=verbs.cmd_update_shell)

    # llms.txt
    llms_p = sub.add_parser(
        "llms.txt",
        help="Print agent instructions.",
        description="Print agent instructions.",
        formatter_class=_Formatter,
    )
    llms_p.set_defaults(func=verbs.cmd_llms_txt)

    return p


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    parser = _make_parser()

    parsed = parser.parse_args(args)
    if not getattr(parsed, "verb", None):
        parser.print_help()
        return 0
    try:
        return parsed.func(parsed)
    except ConfigError as e:
        # Every verb loads config; fail hard on a bad file rather than
        # silently running with defaults.
        print(f"live: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
