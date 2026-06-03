"""Top-level CLI dispatch: `live <verb> ...`."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import verbs


def _lines_arg(value: str) -> tuple[str, int]:
    """Parse `-n` / `--lines`: either `N` (last N lines) or `+N` (lines with n > N)."""
    if value.startswith("+"):
        rest = value[1:]
        if rest.isdigit():
            return ("since", int(rest))
    elif value.isdigit():
        return ("last", int(value))
    raise argparse.ArgumentTypeError(
        f"expected N or +N (got {value!r})"
    )


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live",
        description="Stream long-lived command output to coding agents.",
        add_help=True,
    )
    p.add_argument("--version", action="version", version=f"live {__version__}")
    sub = p.add_subparsers(dest="verb", metavar="<verb>")

    # run
    run_p = sub.add_parser(
        "run", help="Run <cmd> in a PTY, mirror to stdout, record to disk."
    )
    run_p.add_argument("-n", "--name", default=None)
    run_p.add_argument("cmd", nargs=argparse.REMAINDER)
    run_p.set_defaults(func=verbs.cmd_run)

    # ls
    ls_p = sub.add_parser(
        "ls", help="List sessions in working directory (or below)."
    )
    ls_p.add_argument("-a", "--all", action="store_true", help="Include exited.")
    ls_p.add_argument("-g", "--global", action="store_true", dest="global_",
                      help="Global directory scope.")
    ls_p.add_argument("--json", action="store_true",
                      help="Emit NDJSON with full session data.")
    ls_p.add_argument("selector", nargs="?", default=None,
                      help="Optional NAME or UUID-prefix filter.")
    ls_p.set_defaults(func=verbs.cmd_ls)

    # cat
    cat_p = sub.add_parser("cat", help="Concatenate session.")
    cat_p.add_argument("-v", "--verbose", action="store_true",
                       help="Verbose output (for agents).")
    cat_p.add_argument("-g", "--global", action="store_true", dest="global_",
                       help="Global directory scope.")
    ag = cat_p.add_mutually_exclusive_group()
    ag.add_argument("--strip-ansi", action="store_true", dest="strip_ansi",
                    help="Remove ANSI escapes.")
    ag.add_argument("--raw", action="store_true", dest="raw",
                    help="Keep ANSI escapes.")
    cat_p.add_argument("selector")
    cat_p.set_defaults(func=verbs.cmd_cat)

    # tail
    tail_p = sub.add_parser("tail", help="Tail session.")
    tail_p.add_argument("-f", "--follow", action="store_true",
                        help="Follow new lines until exit.")
    tail_p.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output (for agents).")
    tail_p.add_argument("-g", "--global", action="store_true", dest="global_",
                        help="Global directory scope.")
    ag = tail_p.add_mutually_exclusive_group()
    ag.add_argument("--strip-ansi", action="store_true", dest="strip_ansi",
                    help="Remove ANSI escapes.")
    ag.add_argument("--raw", action="store_true", dest="raw",
                    help="Keep ANSI escapes.")
    mode = tail_p.add_mutually_exclusive_group()
    mode.add_argument("-n", "--lines", type=_lines_arg, default=None,
                      help="Last N lines, or +N for lines with n > N (resumable polling).")
    mode.add_argument("-c", "--bytes", dest="bytes_", type=int, default=None,
                      help="Last K bytes.")
    mode.add_argument("--since", type=float, default=None,
                      help="Lines with index timestamp > T (epoch seconds).")
    tail_p.add_argument("selector")
    tail_p.set_defaults(func=verbs.cmd_tail)

    # rm
    rm_p = sub.add_parser("rm", help="Delete sessions.")
    rm_p.add_argument("-f", "--force", action="store_true",
                      help="SIGTERM live runs and ignore nonexistent.")
    rm_p.add_argument("-g", "--global", action="store_true", dest="global_",
                      help="Global directory scope.")
    rm_p.add_argument("--all-exited", action="store_true", dest="all_exited",
                      help="Remove every dead session in scope.")
    rm_p.add_argument("selectors", nargs="*")
    rm_p.set_defaults(func=verbs.cmd_rm)

    # llms.txt
    llms_p = sub.add_parser(
        "llms.txt",
        help="Print token-minimal agent guide for `live tail -vn +N` polling.",
    )
    llms_p.set_defaults(func=verbs.cmd_llms_txt)

    # completion
    comp_p = sub.add_parser("completion", help="Print shell completion script.")
    comp_p.add_argument("shell", choices=["bash", "zsh", "fish"])
    comp_p.set_defaults(func=verbs.cmd_completion)

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
