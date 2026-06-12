"""Shell completion scripts, generated from the CLI parser.

The parser declares the completion metadata (see cli.py): actions carry a
`completion_role` — "selector" positionals, plus the "cwd"/"global"/"all"
flags the selector helpers re-scan from the typed command line — and a verb's
`completion_sessions` default picks which sessions its selectors offer.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Flag:
    short: str | None  # "-a"
    long: str | None  # "--all"
    takes_value: bool
    help: str
    metavar: str
    excludes: tuple[str, ...]  # option strings suppressed alongside this flag
    role: str  # `completion_role`: "cwd", "global", "all", or ""

    @property
    def strings(self) -> tuple[str, ...]:
        return tuple(s for s in (self.short, self.long) if s)

    @property
    def is_cwd(self) -> bool:
        return self.role == "cwd"


@dataclass(frozen=True)
class _Verb:
    name: str
    help: str
    flags: tuple[_Flag, ...]
    selector: str | None  # "1" single slot, "*" variadic, None
    choices: tuple[str, ...]  # static positional candidates
    choices_label: str
    handoff: bool  # positional is a wrapped command (`run`)
    sessions: str  # selector candidates: "all", "mirror" (exited need -a), "running"


# ----- parser walk -----


def _extract() -> tuple[list[_Flag], list[_Verb]]:
    """(top-level flags, verbs in definition order) from the CLI parser."""
    from .cli import _make_parser  # deferred: cli -> verbs -> completion

    parser = _make_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    helps = {a.dest: (a.help or "") for a in sub._choices_actions}
    top = [
        _flag_of(a, {})
        for a in parser._actions
        if a.option_strings and not isinstance(a, argparse._HelpAction)
    ]
    verbs = [_verb_of(name, p, helps.get(name, "")) for name, p in sub.choices.items()]
    if sum(v.handoff for v in verbs) > 1:
        raise ValueError("multiple handoff verbs; the emitters assume at most one")
    _check_plumbing(sub, _scan_flags(verbs))
    return top, verbs


def _check_plumbing(sub, scan: tuple[_Flag, ...]) -> None:
    """The selector helpers shell out to `live completion selectors`,
    forwarding the scan flags' short forms; fail generation if the plumbing
    doesn't accept them."""
    try:
        comp = sub.choices["completion"]
        csub = next(
            a for a in comp._actions if isinstance(a, argparse._SubParsersAction)
        )
        sel = csub.choices["selectors"]
    except (KeyError, StopIteration):
        raise ValueError("missing `live completion selectors` plumbing") from None
    accepted = {s for a in sel._actions for s in a.option_strings}
    for f in scan:
        if f.short not in accepted:
            raise ValueError(f"`completion selectors` does not accept {f.short}")


def _flag_of(action, excludes: tuple[str, ...] = ()) -> _Flag:
    short = next((s for s in action.option_strings if not s.startswith("--")), None)
    long = next((s for s in action.option_strings if s.startswith("--")), None)
    metavar = action.metavar or action.dest
    return _Flag(
        short=short,
        long=long,
        takes_value=action.nargs != 0,  # store_true/store_const have nargs=0
        help=action.help or "",
        metavar=" ".join(metavar) if isinstance(metavar, tuple) else str(metavar),
        excludes=excludes or tuple(s for s in (short, long) if s),
        role=getattr(action, "completion_role", ""),
    )


def _verb_of(name: str, parser, help_: str) -> _Verb:
    mutex: dict[argparse.Action, tuple[str, ...]] = {}
    for group in parser._mutually_exclusive_groups:
        strings = tuple(s for a in group._group_actions for s in a.option_strings)
        for a in group._group_actions:
            mutex[a] = strings
    sessions = parser.get_default("completion_sessions") or "all"
    if sessions not in ("all", "mirror", "running"):
        raise ValueError(f"verb {name!r}: unknown completion_sessions {sessions!r}")
    flags: list[_Flag] = []
    selector = None
    choices: tuple[str, ...] = ()
    choices_label = ""
    handoff = False
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            choices, choices_label = tuple(action.choices), action.dest
        elif action.option_strings:
            flags.append(_flag_of(action, mutex.get(action, ())))
        elif action.nargs == argparse.REMAINDER:
            handoff = True
        elif action.choices is not None:
            choices, choices_label = tuple(action.choices), action.dest
        elif getattr(action, "completion_role", "") == "selector":
            selector = "*" if action.nargs == "*" else "1"
        else:
            raise ValueError(
                f"verb {name!r}: positional {action.dest!r} has no completion strategy"
            )
    if choices and flags:
        raise ValueError(f"verb {name!r}: flags alongside static choices")
    return _Verb(
        name=name,
        help=help_,
        flags=tuple(flags),
        selector=selector,
        choices=choices,
        choices_label=choices_label,
        handoff=handoff,
        sessions=sessions,
    )


# ----- shared emit helpers -----


def _fill(template: str, **subs: str) -> str:
    for key, value in subs.items():
        template = template.replace(f"@{key}@", value)
    return template


def _zsh_text(text: str) -> str:
    """Escape for a single-quoted zsh `_arguments` spec; a literal `]` would
    terminate the description early."""
    return text.replace("'", "'\\''").replace("[", "(").replace("]", ")")


def _zsh_slot(text: str) -> str:
    """Escape for the `:message:` value slot, where `:` separates fields."""
    return _zsh_text(text).replace(":", "\\:")


def _fish_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def _value_flags(verb: _Verb) -> list[_Flag]:
    return [f for f in verb.flags if f.takes_value]


def _flag_strings(flags: list[_Flag]) -> list[str]:
    return [s for f in flags for s in f.strings]


def _cluster_letters(flags: list[_Flag]) -> str:
    """Letters for one-letter cluster classes; multi-char single-dash shorts
    cannot cluster, so they are matched as exact strings instead."""
    return "".join(f.short[1:] for f in flags if f.short and len(f.short) == 2)


def _selector_names(verbs: list[_Verb], sessions: str) -> list[str]:
    return [v.name for v in verbs if v.selector and v.sessions == sessions]


def _count_flags(verbs: list[_Verb]) -> tuple[str, list[str]]:
    """(cluster letters, exact strings) of count/duration value flags — every
    value-taking flag except the cwd flag across the selector verbs."""
    letters: list[str] = []
    exacts: list[str] = []
    for v in verbs:
        if not v.selector:
            continue
        for f in _value_flags(v):
            if f.is_cwd:
                continue
            for s in f.strings:
                if len(s) == 2 and not s.startswith("--"):
                    if s[1:] not in letters:
                        letters.append(s[1:])
                elif s not in exacts:
                    exacts.append(s)
    return "".join(letters), exacts


def _scan_flags(verbs: list[_Verb]) -> tuple[_Flag, _Flag, _Flag]:
    """The (all, global, cwd) flags the selector helpers re-scan from the
    typed command line; each needs a one-letter short and a long form."""
    by_role: dict[str, _Flag] = {}
    for v in verbs:
        for f in v.flags:
            by_role.setdefault(f.role, f)
    found = []
    for role in ("all", "global", "cwd"):
        f = by_role.get(role)
        if f is None:
            raise ValueError(f"no flag tagged with completion role {role!r}")
        if not (f.short and len(f.short) == 2 and f.long):
            raise ValueError(
                f"completion role {role!r} needs a one-letter short and a long form"
            )
        found.append(f)
    return found[0], found[1], found[2]


def _choice_groups(verbs: list[_Verb]) -> list[tuple[list[str], _Verb]]:
    """Verbs with identical static choices, grouped for a shared case arm."""
    groups: dict[tuple, list[_Verb]] = {}
    for v in verbs:
        if v.choices and not v.selector:
            groups.setdefault((v.choices, v.choices_label), []).append(v)
    return [([v.name for v in vs], vs[0]) for vs in groups.values()]


# ----- bash -----

_BASH_HEAD = r"""# bash completion for live
_live_complete() {
    local cur prev words cword
    _init_completion -n : 2>/dev/null || {
        cur="${COMP_WORDS[COMP_CWORD]}"
        prev="${COMP_WORDS[COMP_CWORD-1]}"
        words=("${COMP_WORDS[@]}")
        cword=$COMP_CWORD
    }

    # Find the verb (first non-flag token after `live`).
    local verb verb_idx
    for ((i=1; i<cword; i++)); do
        case "${words[i]}" in
            -*) continue ;;
            *) verb="${words[i]}"; verb_idx=$i; break ;;
        esac
    done

    if [ -z "$verb" ]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "@TOP_FLAGS@" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "@VERBS@" -- "$cur") )
        fi
        return
    fi

    # Complete a cwd-flag value, except inside the wrapped command
    # (the handoff below owns that).
    if @CWD_GUARD@; then
        _live_complete_cwd; return
    fi

    case "$verb" in
"""

_BASH_RUN_ARM = r"""        @NAME@)
            # If the user has typed a non-flag arg after the verb, hand off
            # to the wrapped command's completion via _command_offset.
            local seen_cmd=0
            for ((i=verb_idx+1; i<cword; i++)); do
                case "${words[i]}" in
                    --) seen_cmd=$((i+1)); break ;;
                    # Value-taking flags, exact or cluster-final (`-dn NAME`).
                    @VALUE_PATS@)
                        i=$((i+1))
                        # `--opt=value` arrives split: ("--opt" "=" "value").
                        [ "${words[i]}" = "=" ] && i=$((i+1))
                        ;;
                    -*) ;;
                    *) seen_cmd=$i; break ;;
                esac
            done
            if [ "$seen_cmd" -gt 0 ] && type _command_offset >/dev/null 2>&1; then
                _command_offset $seen_cmd
                return
            fi
            if _live_prev_is_cwd; then
                _live_complete_cwd; return
            fi
            COMPREPLY=( $(compgen -W "@FLAG_WORDS@" -- "$cur") )
            ;;
"""

_BASH_PREV_IS_CWD = r"""
# True when the cursor sits on a cwd-flag value. Readline splits
# `--opt=value` on `=`, so a `=` prev means look one word further back.
_live_prev_is_cwd() {
    case "$prev" in
        @CWD_ALTS@) return 0 ;;
        =) case "${words[cword-2]}" in @CWD_ALTS@) return 0 ;; esac ;;
    esac
    return 1
}
"""

_BASH_PREV_IS_COUNT = r"""
# True when the cursor sits on the value of a count/duration flag (any
# value-taking flag except the cwd flag), exact, cluster-final (`-fn`), or
# split on `=`; selectors must not be offered there.
_live_prev_is_count() {
    case "$prev" in
        @PREV_PATS@) return 0 ;;
        =) case "${words[cword-2]}" in @EXACT_PATS@) return 0 ;; esac ;;
    esac
    return 1
}
"""

_BASH_TAIL = r"""
# Prints the all flag's short form when its long form, short form, or a
# short cluster containing its letter was typed; letters inside an attached
# cwd value don't count.
_live_all_flag() {
    local i
    for ((i=1; i<cword; i++)); do
        case "${words[i]}" in
            @ALL_LONG@) echo -@AL@; return ;;
            --*) ;;
            -*) case "${words[i]%%@CW@*}" in *@AL@*) echo -@AL@; return ;; esac ;;
        esac
    done
}

# Append stdin lines matching the current prefix to COMPREPLY, verbatim
# (no word splitting or expansion of candidate text).
_live_reply_lines() {
    local line
    while IFS= read -r line; do
        [[ "$line" == "$cur"* ]] && COMPREPLY+=("$line")
    done
}

# Selector completion; arguments plus any typed scope flag (global, or
# cwd with its value, clustered or attached) are forwarded to
# `live completion selectors`, along with the typed prefix so ids are
# offered only when no name matches.
_live_complete_selectors() {
    local -a sel_args=("$@")
    local i j w cluster
    for ((i=1; i<cword; i++)); do
        w="${words[i]}"
        case "$w" in
            @GLOBAL_LONG@) sel_args+=(-@G@) ;;
            @CWD_ALTS@)
                j=$((i+1))
                [ "${words[j]}" = "=" ] && j=$((j+1))
                (( j < cword )) && sel_args+=(-@CW@ "${words[j]}")
                ;;
            --*) ;;
            -?*)
                cluster="${w%%@CW@*}"  # short flags before any cwd value
                case "$cluster" in *@G@*) sel_args+=(-@G@) ;; esac
                if [ "$w" != "$cluster" ]; then
                    if [ -n "${w##*@CW@}" ]; then
                        sel_args+=(-@CW@ "${w#*@CW@}")  # attached: -CPATH
                    else
                        j=$((i+1))  # cluster-final: -aC PATH
                        [ "${words[j]}" = "=" ] && j=$((j+1))
                        (( j < cword )) && sel_args+=(-@CW@ "${words[j]}")
                    fi
                fi
                ;;
        esac
    done
    COMPREPLY=()
    _live_reply_lines < <(live completion selectors "${sel_args[@]}" -- "$cur" 2>/dev/null)
}

# cwd value completion: cwds of recorded sessions; plain directories only
# when no recorded cwd matches the typed prefix.
_live_complete_cwd() {
    COMPREPLY=()
    _live_reply_lines < <(live completion cwds 2>/dev/null)
    if [ ${#COMPREPLY[@]} -eq 0 ]; then
        local IFS=$'\n'
        COMPREPLY=( $(compgen -d -- "$cur") )
    fi
    type compopt >/dev/null 2>&1 && compopt -o filenames
    return 0
}

complete -F _live_complete live
"""


def _bash_selector_call(verb: _Verb, al: str) -> str:
    if verb.sessions == "running":
        return "_live_complete_selectors"
    if verb.sessions == "mirror":
        return "_live_complete_selectors $(_live_all_flag)"
    return f"_live_complete_selectors -{al}"


def _bash_verb_arm(verb: _Verb, al: str) -> str:
    flag_words = " ".join(_flag_strings(verb.flags))
    lines = [
        f"        {verb.name})",
        '            if [[ "$cur" == -* ]]; then',
        f'                COMPREPLY=( $(compgen -W "{flag_words}" -- "$cur") )',
    ]
    if verb.selector:
        if any(f.takes_value and not f.is_cwd for f in verb.flags):
            middle = "elif ! _live_prev_is_count; then"
        else:
            middle = "else"
        lines += [
            f"            {middle}",
            f"                {_bash_selector_call(verb, al)}",
        ]
    lines += ["            fi", "            ;;"]
    return "\n".join(lines) + "\n"


def _bash(top: list[_Flag], verbs: list[_Verb], scan: tuple[_Flag, ...]) -> str:
    al, gl, cwd = scan
    handoff = next((v for v in verbs if v.handoff), None)
    cwd_guard = "_live_prev_is_cwd"
    if handoff is not None:
        cwd_guard = f'[ "$verb" != {handoff.name} ] && _live_prev_is_cwd'
    out = _fill(
        _BASH_HEAD,
        TOP_FLAGS=" ".join(_flag_strings(top)),
        VERBS=" ".join(v.name for v in verbs),
        CWD_GUARD=cwd_guard,
    )
    for v in verbs:
        if v.handoff:
            vflags = _value_flags(v)
            pats = _flag_strings(vflags)
            letters = _cluster_letters(vflags)
            if letters:
                pats.append(f"-[!-{letters}]*[{letters}]")
            out += _fill(
                _BASH_RUN_ARM,
                NAME=v.name,
                VALUE_PATS="|".join(pats) or "''",
                FLAG_WORDS=" ".join([*_flag_strings(v.flags), "--"]),
            )
        elif v.selector or v.flags:
            out += _bash_verb_arm(v, al.short[1:])
    for names, rep in _choice_groups(verbs):
        out += (
            f"        {'|'.join(names)})\n"
            f'            COMPREPLY=( $(compgen -W "{" ".join(rep.choices)}" -- "$cur") )\n'
            f"            ;;\n"
        )
    out += "    esac\n}\n"
    out += _fill(_BASH_PREV_IS_CWD, CWD_ALTS="|".join(cwd.strings))
    letters, exacts = _count_flags(verbs)
    exact = ([f"-[{letters}]"] if letters else []) + exacts
    if exact:
        prev = list(exact)
        if letters:
            prev.append(f"-[!-{letters}{cwd.short[1:]}]*[{letters}]")
        out += _fill(
            _BASH_PREV_IS_COUNT,
            PREV_PATS="|".join(prev),
            EXACT_PATS="|".join(exact),
        )
    out += _fill(
        _BASH_TAIL,
        ALL_LONG=al.long,
        AL=al.short[1:],
        GLOBAL_LONG=gl.long,
        G=gl.short[1:],
        CWD_ALTS="|".join(cwd.strings),
        CW=cwd.short[1:],
    )
    return out


# ----- zsh -----

_ZSH_HEAD = r"""#compdef live

_live() {
    local context state line
    typeset -A opt_args

    _arguments -C \
        @TOP_SPECS@ \
        '1: :_live_verbs' \
        '*::arg:->args' \
        && return 0

    case $state in
        args)
            case $words[1] in
@ARMS@            esac
            ;;
    esac
}

_live_verbs() {
    local -a verbs
    verbs=(
@VERB_LINES@
    )
    _describe -t verbs 'verb' verbs
}
"""

_ZSH_TAIL = r"""
# Selector completion. Which sessions are offered depends on the verb (see
# the generated case below). Honors the global and cwd scope flags when
# present in the command line, clustered (`-ag`, `-aC <dir>`) or attached
# (`-C<dir>`). `$PREFIX` is forwarded so ids are offered only when no name
# matches.
_live_selectors() {
    local -a sel_args names
    local i cluster want_all=0 want_global=0
    for (( i=2; i <= $#words; i++ )); do
        case $words[i] in
            @ALL_ALTS@) want_all=1 ;;
            @GLOBAL_ALTS@) want_global=1 ;;
            @CWD_ALTS@) (( i < $#words )) && sel_args+=(-@CW@ "$words[i+1]") ;;
            @CWD_LONG@=*) sel_args+=(-@CW@ "${words[i]#@CWD_LONG@=}") ;;
            -@CW@*) sel_args+=(-@CW@ "${words[i]#-@CW@}") ;;
            --*) ;;
            -*)
                cluster="${words[i]%%@CW@*}"  # short flags before any cwd value
                [[ $cluster == *@AL@* ]] && want_all=1
                [[ $cluster == *@G@* ]] && want_global=1
                if [[ $words[i] != "$cluster" ]]; then
                    if [[ -n ${words[i]##*@CW@} ]]; then
                        sel_args+=(-@CW@ "${words[i]#*@CW@}")  # attached: -aC<dir>
                    else
                        (( i < $#words )) && sel_args+=(-@CW@ "$words[i+1]")
                    fi
                fi
                ;;
        esac
    done
    case $words[1] in
@POLICY@    esac
    (( want_global )) && sel_args+=(-@G@)
    names=( ${(f)"$(live completion selectors $sel_args -- "$PREFIX" 2>/dev/null)"} )
    (( $#names )) && _values 'selector' "${names[@]}"
}

# cwd value completion: cwds of recorded sessions; plain directories only
# when no recorded cwd matches the typed prefix (compadd fails when nothing
# it added matched).
_live_cwds() {
    local -a cwds
    cwds=( ${(f)"$(live completion cwds 2>/dev/null)"} )
    (( $#cwds )) && compadd -- "${cwds[@]}" && return
    _files -/
}

_live "$@"
"""


def _zsh_flag(f: _Flag) -> str:
    excl = "(" + " ".join(f.excludes) + ")"
    desc = f"[{_zsh_text(f.help)}]" if f.help else ""
    if f.takes_value:
        action = "_live_cwds" if f.is_cwd else ""
        # The slot message is what zsh shows while completing the value, so
        # carry the help text there, not just the metavar.
        slot = f":{_zsh_slot(f.help or f.metavar)}:{action}"
        if f.short and f.long:
            return f"'{excl}'{{{f.short}+,{f.long}=}}'{desc}{slot}'"
        if f.long:
            return f"'{excl}{f.long}={desc}{slot}'"
        return f"'{excl}{f.short}+{desc}{slot}'"
    if f.short and f.long:
        return f"'{excl}'{{{f.short},{f.long}}}'{desc}'"
    return f"'{excl}{f.long or f.short}{desc}'"


def _zsh_verb_arm(verb: _Verb) -> str:
    specs = [_zsh_flag(f) for f in verb.flags]
    if verb.handoff:
        # `-S` stops at `--`; any non-flag word hands off to `_normal`
        # against the wrapped command. `-s` parses clustered short flags.
        opts = "-s -S"
        specs.append("'*::command:_normal'")
    else:
        opts = "-s"
        if verb.selector:
            specs.append(f"'{verb.selector}:selector:_live_selectors'")
    body = " \\\n                        ".join(specs)
    return (
        f"                {verb.name})\n"
        f"                    _arguments {opts} \\\n"
        f"                        {body}\n"
        f"                    ;;\n"
    )


def _zsh_policy(verbs: list[_Verb], al: str) -> str:
    running = _selector_names(verbs, "running")
    mirror = _selector_names(verbs, "mirror")
    lines = []
    if running:
        lines.append(f"        {'|'.join(running)}) ;;")
    if mirror:
        lines.append(
            f"        {'|'.join(mirror)}) (( want_all )) && sel_args+=(-{al}) ;;"
        )
    lines.append(f"        *) sel_args+=(-{al}) ;;")
    return "\n".join(lines) + "\n"


def _zsh(top: list[_Flag], verbs: list[_Verb], scan: tuple[_Flag, ...]) -> str:
    al, gl, cwd = scan
    arms = ""
    for v in verbs:
        if v.handoff or v.selector or v.flags:
            arms += _zsh_verb_arm(v)
    for names, rep in _choice_groups(verbs):
        arms += (
            f"                {'|'.join(names)})\n"
            f"                    _arguments "
            f"'1:{rep.choices_label}:({' '.join(rep.choices)})'\n"
            f"                    ;;\n"
        )
    verb_lines = "\n".join(f"        '{v.name}:{_zsh_text(v.help)}'" for v in verbs)
    out = _fill(
        _ZSH_HEAD,
        TOP_SPECS=" \\\n        ".join(_zsh_flag(f) for f in top),
        ARMS=arms,
        VERB_LINES=verb_lines,
    )
    out += _fill(
        _ZSH_TAIL,
        POLICY=_zsh_policy(verbs, al.short[1:]),
        ALL_ALTS="|".join(al.strings),
        GLOBAL_ALTS="|".join(gl.strings),
        CWD_ALTS="|".join(cwd.strings),
        CWD_LONG=cwd.long,
        AL=al.short[1:],
        G=gl.short[1:],
        CW=cwd.short[1:],
    )
    return out


# ----- fish -----

_FISH_HEAD = r"""# fish completion for live

# True when a short-flag cluster contains the letter $argv[1] (`-ag`);
# letters inside an attached cwd value don't count.
function __live_cluster_has
    for t in $argv[2..-1]
        string match -qr -- "^-[^-@CW@]*$argv[1]" $t; and return 0
    end
    return 1
end

function __live_selectors
    set -l toks (commandline -opc)
    set -l verb $toks[2]
    set -l args
@POLICY@    if contains -- @GLOBAL_LONG@ $toks; or __live_cluster_has @G@ $toks
        set -a args -@G@
    end
    # cwd value: next token (exact or cluster-final), --opt=DIR, or attached.
    set -l n (count $toks)
    set -l i 2
    while test $i -le $n
        set -l t $toks[$i]
        if test "$t" = @CWD_LONG@; or string match -qr -- '^-[^-]*@CW@$' $t
            test $i -lt $n; and set -a args -@CW@ $toks[(math $i + 1)]
        else if string match -qr -- '^@CWD_LONG@=.' $t
            set -a args -@CW@ (string replace -- @CWD_LONG@= '' $t)
        else if string match -qr -- '^-@CW@.' $t
            set -a args -@CW@ (string sub -s 3 -- $t)
        end
        set i (math $i + 1)
    end
    # Forward the typed prefix; ids are offered only when no name matches.
    live completion selectors $args -- (commandline -ct) 2>/dev/null
end

# cwd value completion: cwds of recorded sessions; plain directories only
# when no recorded cwd matches the typed prefix.
function __live_cwds
    set -l cur (commandline -ct)
    set -l hits (live completion cwds 2>/dev/null | string match -- "$cur*")
    if set -q hits[1]
        printf '%s\n' $hits
    else
        __fish_complete_directories
    end
end

# True while the cursor is still on the verb's own flags — before the
# wrapped command's first token.
function __live_run_needs_flags
    set -l toks (commandline -opc)
    set -l i 3
    while test $i -le (count $toks)
        switch $toks[$i]
            case --
                return 1
            case @VALUE_FLAGS@
                set i (math $i + 2)
            case '-*'
                @CLUSTER_SKIP@
            case '*'
                return 1
        end
    end
    return 0
end

# The active verb: first non-flag token after `live`.
function __live_verb
    set -l toks (commandline -opc)
    for t in $toks[2..-1]
        string match -q -- '-*' $t; and continue
        echo $t
        return
    end
end

function __live_verb_is
    contains -- "$(__live_verb)" $argv
end

set -l verbs @VERBS@

complete -c live -f
"""


def _fish_flag(cond: str, f: _Flag) -> str:
    parts = [f'complete -c live -n "{cond}"']
    if f.short:
        # `-o` declares old-style (single-dash, multi-char) options.
        parts.append(f"{'-s' if len(f.short) == 2 else '-o'} {f.short[1:]}")
    if f.long:
        parts.append(f"-l {f.long[2:]}")
    if f.takes_value:
        parts.append('-x -a "(__live_cwds)"' if f.is_cwd else "-r")
    if f.help:
        parts.append(f"-d '{_fish_text(f.help)}'")
    return " ".join(parts)


def _fish_policy(verbs: list[_Verb], al: _Flag) -> str:
    running = _selector_names(verbs, "running")
    mirror = _selector_names(verbs, "mirror")
    a = al.short[1:]
    all_cond = "true"
    if mirror:
        all_cond = (
            f'not contains -- "$verb" {" ".join(mirror)};'
            f" or contains -- {al.long} $toks; or __live_cluster_has {a} $toks"
        )
    if running:
        return (
            f'    if contains -- "$verb" {" ".join(running)}\n'
            "        # active sessions only\n"
            f"    else if {all_cond}\n"
            f"        set -a args -{a}\n"
            "    end\n"
        )
    return f"    if {all_cond}\n        set -a args -{a}\n    end\n"


def _fish_cluster_skip(letters: str) -> str:
    if not letters:
        return "set i (math $i + 1)"
    return (
        "# A cluster ending in a value-taking flag skips its value too.\n"
        f"                if string match -qr -- '^-[^-{letters}][^-]*[{letters}]$'"
        " $toks[$i]\n"
        "                    set i (math $i + 2)\n"
        "                else\n"
        "                    set i (math $i + 1)\n"
        "                end"
    )


def _fish(top: list[_Flag], verbs: list[_Verb], scan: tuple[_Flag, ...]) -> str:
    al, gl, cwd = scan
    handoff = next((v for v in verbs if v.handoff), None)
    run_vflags = _value_flags(handoff) if handoff else []
    letters = _cluster_letters(run_vflags)
    out = _fill(
        _FISH_HEAD,
        POLICY=_fish_policy(verbs, al),
        VALUE_FLAGS=" ".join(_flag_strings(run_vflags)) or "''",
        CLUSTER_SKIP=_fish_cluster_skip(letters),
        VERBS=" ".join(v.name for v in verbs),
        GLOBAL_LONG=gl.long,
        G=gl.short[1:],
        CWD_LONG=cwd.long,
        CW=cwd.short[1:],
    )
    for v in verbs:
        out += (
            f'complete -c live -n "not __live_verb_is $verbs" -a {v.name}'
            f" -d '{_fish_text(v.help)}'\n"
        )
    for f in top:
        out += _fish_flag("not __live_verb_is $verbs", f) + "\n"

    sel_verbs = " ".join(v.name for v in verbs if v.selector)
    out += (
        f"\n# Selector completion for {sel_verbs.replace(' ', ' / ')}.\n"
        f'complete -c live -n "__live_verb_is {sel_verbs}" -a "(__live_selectors)"\n'
    )

    for v in verbs:
        if v.handoff or not v.flags:
            continue
        out += f"\n# {v.name}\n"
        for f in v.flags:
            out += _fish_flag(f"__live_verb_is {v.name}", f) + "\n"

    if handoff is not None:
        cond = f"__live_verb_is {handoff.name}; and __live_run_needs_flags"
        out += (
            f"\n# {handoff.name} -- own flags before the wrapped command;"
            " hand off afterwards.\n"
        )
        for f in handoff.flags:
            out += _fish_flag(cond, f) + "\n"
        skip = _flag_strings(run_vflags)
        bools = _cluster_letters([f for f in handoff.flags if not f.takes_value])
        skip += [f"-{b}{v}" for v in letters for b in bools]
        out += (
            "# Trailing args are value-taking flags to skip; the helper matches\n"
            "# tokens exactly, so clustered forms must be listed too.\n"
            f'complete -c live -n "__live_verb_is {handoff.name}" \\\n'
            f'    -a "(__fish_complete_subcommand --fcs-skip=2 {" ".join(skip)})"\n'
        )

    out += "\n"
    for names, rep in _choice_groups(verbs):
        out += (
            f'complete -c live -n "__live_verb_is {" ".join(names)}"'
            f' -a "{" ".join(rep.choices)}"\n'
        )
    return out


# ----- entry points -----

_EMITTERS = {"bash": _bash, "zsh": _zsh, "fish": _fish}


def script_for(shell: str) -> str | None:
    emit = _EMITTERS.get(shell)
    if emit is None:
        return None
    top, verbs = _extract()
    script = emit(top, verbs, _scan_flags(verbs))
    unfilled = re.search(r"@[A-Z_]+@", script)
    if unfilled:
        raise ValueError(f"unfilled template key {unfilled.group()} in {shell} script")
    return script


# `update-shell` installs these loaders instead of the payloads above, so
# completions always track the installed `live` and updating it never
# requires rerunning `update-shell`. Each shell evaluates its loader lazily
# (bash-completion, compinit autoload, fish autoload), costing one `live`
# invocation per shell session; if `live` is off $PATH the loader degrades
# to no completions and retries in the next session.

BASH_LOADER = 'eval "$(live completion-script bash 2>/dev/null)"\n'

# Autoloaded as `_live`'s function body: the eval (re)defines `_live` and
# its helpers, then the payload's trailing `_live "$@"` completes the
# current word.
ZSH_LOADER = '#compdef live\neval "$(live completion-script zsh 2>/dev/null)"\n'

FISH_LOADER = "live completion-script fish 2>/dev/null | source\n"


def loader_for(shell: str) -> str | None:
    return {"bash": BASH_LOADER, "zsh": ZSH_LOADER, "fish": FISH_LOADER}.get(shell)
