"""Top-level CLI dispatch: `live <verb> ...`."""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime

from . import __version__
from . import verbs

_AGE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([dhms])\s*$")


def _parse_age(value: str) -> float:
    """Return a cutoff in epoch seconds. Sessions exited before it count as older.

    Duration form: `7d`, `12h`, `30m`, `60s` → `now - N`.
    Absolute form: ISO date/datetime (`2026-01-01`, `2026-01-01T12:00:00`); naive
    timestamps are interpreted as local time.
    """
    m = _AGE_RE.match(value)
    if m:
        n = float(m.group(1))
        unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}[m.group(2)]
        return time.time() - n * unit
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected duration (e.g. 7d, 12h, 30m, 60s) or ISO datetime (got {value!r})"
        )


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


class _Formatter(argparse.HelpFormatter):
    """Render `REMAINDER` positionals using their metavar instead of `...`."""

    def _format_args(self, action, default_metavar):
        if action.nargs == argparse.REMAINDER:
            return "%s ..." % self._metavar_formatter(action, default_metavar)(1)
        return super()._format_args(action, default_metavar)


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live",
        description="Live stream command line output. Inspect long processes from agent workflows.",
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
    run_p.add_argument("-n", "--name", default=None, help="Session name.")
    run_p.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        metavar="cmd",
        help="Command to run; `--` for flag-starting commands.",
    )
    run_p.set_defaults(func=verbs.cmd_run)

    # ls
    ls_p = sub.add_parser(
        "ls",
        help="List sessions in scope.",
        description="List recorded sessions.",
        formatter_class=_Formatter,
    )
    ls_p.add_argument("-a", "--all", action="store_true", help="Include exited.")
    ls_p.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_",
        help="Global scope.",
    )
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
    cat_p.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_",
        help="Global scope.",
    )
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
    head_p.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_",
        help="Global scope.",
    )
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
        type=float,
        default=None,
        help="Lines with idx t <= T (epoch).",
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
    tail_p.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_",
        help="Global scope.",
    )
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
        help="Last K bytes; +K for bytes after offset K.",
    )
    mode.add_argument(
        "-t",
        "--time",
        type=float,
        default=None,
        help="Lines with idx t > T (epoch).",
    )
    tail_p.add_argument("selector", help="NAME or UUID-prefix.")
    tail_p.set_defaults(func=verbs.cmd_tail)

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
    rm_p.add_argument(
        "-g",
        "--global",
        action="store_true",
        dest="global_",
        help="Global scope.",
    )
    rm_p.add_argument(
        "--all",
        action="store_true",
        dest="all_",
        help="Delete all sessions in scope.",
    )
    rm_p.add_argument(
        "--exited",
        action="store_true",
        help="Delete sessions that have exited.",
    )
    rm_p.add_argument(
        "--untitled",
        action="store_true",
        help="Delete sessions without a name.",
    )
    rm_p.add_argument(
        "--older-than",
        type=_parse_age,
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
        help="Print shell completion script.",
        description="Print a shell completion script.",
        formatter_class=_Formatter,
    )
    comp_p.add_argument("shell", choices=["bash", "zsh", "fish"], help="Target shell.")
    comp_p.set_defaults(func=verbs.cmd_completion)

    # update-shell
    up_p = sub.add_parser(
        "update-shell",
        help="Install completion for the current shell.",
        description="Install shell completion.",
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
        help="Print agent guide.",
        description="Display the agent guide for live.",
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
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
