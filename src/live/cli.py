"""Top-level CLI dispatch: `live <verb> ...`."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import verbs


def _make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="live",
        description="Stream long-lived command output to coding agents.",
        add_help=True,
    )
    p.add_argument("--version", action="version", version=f"live {__version__}")
    sub = p.add_subparsers(dest="verb", metavar="<verb>")

    # run
    run_p = sub.add_parser("run", help="Wrap <cmd> under a PTY and record.")
    run_p.add_argument("-n", "--name", default=None)
    run_p.add_argument("cmd", nargs=argparse.REMAINDER)
    run_p.set_defaults(func=verbs.cmd_run)

    # ls
    ls_p = sub.add_parser("ls", help="List sessions in scope.")
    ls_p.add_argument("-n", "--name", default=None, help="Filter to this NAME.")
    ls_p.add_argument("-a", "--all", action="store_true", help="Include exited.")
    ls_p.add_argument("--json", action="store_true", help="Emit NDJSON.")
    ls_p.set_defaults(func=verbs.cmd_ls)

    # cat
    cat_p = sub.add_parser("cat", help="Concatenate stream.*.log for a session.")
    cat_p.add_argument("-v", "--verbose", action="store_true")
    ag = cat_p.add_mutually_exclusive_group()
    ag.add_argument("--strip-ansi", action="store_true", dest="strip_ansi")
    ag.add_argument("--raw", action="store_true", dest="raw")
    cat_p.add_argument("selector")
    cat_p.set_defaults(func=verbs.cmd_cat)

    # tail
    tail_p = sub.add_parser("tail", help="Tail a session.")
    tail_p.add_argument("-v", "--verbose", action="store_true")
    tail_p.add_argument("-f", "--follow", action="store_true",
                        help="Follow new lines until the recorder exits.")
    ag = tail_p.add_mutually_exclusive_group()
    ag.add_argument("--strip-ansi", action="store_true", dest="strip_ansi")
    ag.add_argument("--raw", action="store_true", dest="raw")
    mode = tail_p.add_mutually_exclusive_group()
    mode.add_argument("-n", "--lines", type=int, default=None,
                      help="Print the last N lines.")
    mode.add_argument("-c", "--bytes", dest="bytes_", type=int, default=None,
                      help="Print the last K bytes.")
    mode.add_argument("--since-line", dest="since_line", type=int, default=None,
                      help="Output lines with n > N (resumable polling).")
    tail_p.add_argument("selector")
    tail_p.set_defaults(func=verbs.cmd_tail)

    # rm
    rm_p = sub.add_parser("rm", help="Delete sessions.")
    rm_p.add_argument("-f", "--force", action="store_true",
                      help="SIGTERM running recorders; ignore nonexistent.")
    rm_p.add_argument("--all-exited", action="store_true", dest="all_exited",
                      help="Remove every dead session in scope.")
    rm_p.add_argument("selectors", nargs="*")
    rm_p.set_defaults(func=verbs.cmd_rm)

    # init
    init_p = sub.add_parser("init", help="Create .live/ in cwd.")
    init_p.set_defaults(func=verbs.cmd_init)

    # llms.txt
    llms_p = sub.add_parser("llms.txt", help="Print the agent guide snippet.")
    llms_p.set_defaults(func=verbs.cmd_llms_txt)

    # completion
    comp_p = sub.add_parser("completion", help="Print shell completion script.")
    comp_p.add_argument("shell", choices=["bash", "zsh", "fish"])
    comp_p.set_defaults(func=verbs.cmd_completion)

    return p


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    parser = _make_parser()

    # `live run` consumes everything after its flags as the wrapped command.
    # argparse.REMAINDER already handles `--`, but we also need to allow flags
    # in the wrapped command (e.g. `live run -n dev npm run dev`). The default
    # argparse handling works because `npm` is non-flag and from there
    # REMAINDER eats the rest.
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
