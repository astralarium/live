"""Shell completion script payloads, returned by `live completion <shell>`.

Each script offers verb completion, per-verb flag completion, selector
completion (NAME or UUID via `live ls --json`), and `live run <TAB>` handoff
to the wrapped command's completion. The selector helper mirrors the verb's
scope flags: `ls` only suggests active sessions unless `-a` was typed;
`cat`/`tail`/`rm` always pass `-a` since exited sessions remain valid
targets. `-g` is honored when present in the command line.
"""

from __future__ import annotations


BASH = r"""# bash completion for live
_live_complete() {
    local cur prev words cword
    _init_completion -n : 2>/dev/null || {
        cur="${COMP_WORDS[COMP_CWORD]}"
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
        COMPREPLY=( $(compgen -W "run ls cat head tail rm llms.txt completion" -- "$cur") )
        return
    fi

    case "$verb" in
        run)
            # If the user has typed a non-flag arg after `run`, hand off to the wrapped
            # command's completion via _command_offset.
            local seen_cmd=0
            for ((i=verb_idx+1; i<cword; i++)); do
                case "${words[i]}" in
                    --) seen_cmd=$((i+1)); break ;;
                    -n) i=$((i+1)) ;;
                    -*) ;;
                    *) seen_cmd=$i; break ;;
                esac
            done
            if [ "$seen_cmd" -gt 0 ] && type _command_offset >/dev/null 2>&1; then
                _command_offset $seen_cmd
                return
            fi
            COMPREPLY=( $(compgen -W "-n --" -- "$cur") )
            ;;
        ls)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-a --all -g --global --json" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_selectors $(_live_all_flag) $(_live_global_flag))" -- "$cur") )
            fi
            ;;
        cat)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -g --global --strip-ansi --raw" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_selectors -a $(_live_global_flag))" -- "$cur") )
            fi
            ;;
        head)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -g --global --strip-ansi --raw -n --lines -c --bytes" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_selectors -a $(_live_global_flag))" -- "$cur") )
            fi
            ;;
        tail)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -f --follow -g --global --strip-ansi --raw -n --lines -c --bytes --since" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_selectors -a $(_live_global_flag))" -- "$cur") )
            fi
            ;;
        rm)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-f --force -g --global --all-exited" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_selectors -a $(_live_global_flag))" -- "$cur") )
            fi
            ;;
        completion)
            COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
            ;;
    esac
}

_live_all_flag() {
    local w
    for w in "${words[@]:1}"; do
        case "$w" in -a|--all) echo -a; return ;; esac
    done
}

_live_global_flag() {
    local w
    for w in "${words[@]:1}"; do
        case "$w" in -g|--global) echo -g; return ;; esac
    done
}

_live_selectors() {
    live ls "$@" --json 2>/dev/null \
        | awk -F'"' '{ for (i=1;i<=NF;i++) if ($i=="id"||$i=="name") print $(i+2) }' \
        | sort -u
}

complete -F _live_complete live
"""


ZSH = r"""#compdef live

_live() {
    local context state line
    typeset -A opt_args

    _arguments -C \
        '1: :_live_verbs' \
        '*::arg:->args' \
        && return 0

    case $state in
        args)
            case $words[1] in
                run)
                    # Complete our flag before the wrapped command; any non-flag
                    # word triggers `_normal` against the wrapped command.
                    _arguments -S \
                        '-n+[session name]:name:' \
                        '*::command:_normal'
                    ;;
                ls)
                    _arguments \
                        '(-a --all)'{-a,--all} \
                        '(-g --global)'{-g,--global} \
                        '--json' \
                        '1:selector:_live_selectors'
                    ;;
                cat)
                    _arguments \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-g --global)'{-g,--global} \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '1:selector:_live_selectors'
                    ;;
                head)
                    _arguments \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-g --global)'{-g,--global} \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '(-n --lines)'{-n+,--lines=}':lines:' \
                        '(-c --bytes)'{-c+,--bytes=}':bytes:' \
                        '1:selector:_live_selectors'
                    ;;
                tail)
                    _arguments \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-f --follow)'{-f,--follow} \
                        '(-g --global)'{-g,--global} \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '(-n --lines)'{-n+,--lines=}':lines:' \
                        '(-c --bytes)'{-c+,--bytes=}':bytes:' \
                        '--since=:epoch-seconds:' \
                        '1:selector:_live_selectors'
                    ;;
                rm)
                    _arguments \
                        '(-f --force)'{-f,--force} \
                        '(-g --global)'{-g,--global} \
                        '--all-exited' \
                        '*:selector:_live_selectors'
                    ;;
                completion)
                    _arguments '1:shell:(bash zsh fish)'
                    ;;
            esac
            ;;
    esac
}

_live_verbs() {
    local -a verbs
    verbs=(
        'run:Wrap <cmd> under a PTY and record'
        'ls:List sessions in scope'
        'cat:Concatenate stream.*.log for a session'
        'head:Head first lines of a session'
        'tail:Tail a session'
        'rm:Delete sessions'
        'llms.txt:Print a token-minimal agent guide'
        'completion:Print shell completion script'
    )
    _describe -t verbs 'verb' verbs
}

# Selector completion. For `live ls`, only suggest active sessions unless -a
# was typed; for other verbs, always include exited (still valid targets).
# Honors -g/--global when present in the command line.
_live_selectors() {
    local -a sel_args names
    local w want_all=0 want_global=0
    for w in $words[@]; do
        case $w in
            -a|--all) want_all=1 ;;
            -g|--global) want_global=1 ;;
        esac
    done
    if [[ $words[1] != ls ]] || (( want_all )); then
        sel_args+=(-a)
    fi
    (( want_global )) && sel_args+=(-g)
    names=( ${(f)"$(live ls $sel_args --json 2>/dev/null | awk -F'"' '{ for (i=1;i<=NF;i++) if ($i=="id"||$i=="name") print $(i+2) }' | sort -u)"} )
    (( $#names )) && _values 'selector' "${names[@]}"
}

_live "$@"
"""


FISH = r"""# fish completion for live

function __live_selectors
    set -l toks (commandline -opc)
    set -l verb $toks[2]
    set -l args
    if test "$verb" != "ls"; or contains -- -a $toks; or contains -- --all $toks
        set -a args -a
    end
    if contains -- -g $toks; or contains -- --global $toks
        set -a args -g
    end
    live ls $args --json 2>/dev/null | string match -rga '"(?:id|name)":"([^"]+)"' | sort -u
end

set -l verbs run ls cat head tail rm llms.txt completion

complete -c live -f
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a run -d 'Wrap <cmd> under a PTY'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a ls -d 'List sessions'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a cat -d 'Concatenate stream.*.log'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a head -d 'Head first lines of a session'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a tail -d 'Tail a session'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a rm -d 'Delete sessions'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a llms.txt -d 'Print agent guide'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a completion -d 'Print completion script'

# Selector completion for ls / cat / head / tail / rm.
complete -c live -n "__fish_seen_subcommand_from ls cat head tail rm" -a "(__live_selectors)"

# ls
complete -c live -n "__fish_seen_subcommand_from ls" -s a -l all -d 'Include exited sessions'
complete -c live -n "__fish_seen_subcommand_from ls" -s g -l global -d 'Show sessions from all directories'
complete -c live -n "__fish_seen_subcommand_from ls" -l json -d 'Emit NDJSON'

# cat
complete -c live -n "__fish_seen_subcommand_from cat" -s v -l verbose -d 'Add stderr metadata'
complete -c live -n "__fish_seen_subcommand_from cat" -s g -l global -d 'Resolve selector globally'
complete -c live -n "__fish_seen_subcommand_from cat" -l strip-ansi -d 'Remove ANSI escapes'
complete -c live -n "__fish_seen_subcommand_from cat" -l raw -d 'Keep ANSI escapes'

# head
complete -c live -n "__fish_seen_subcommand_from head" -s v -l verbose
complete -c live -n "__fish_seen_subcommand_from head" -s g -l global -d 'Resolve selector globally'
complete -c live -n "__fish_seen_subcommand_from head" -l strip-ansi
complete -c live -n "__fish_seen_subcommand_from head" -l raw
complete -c live -n "__fish_seen_subcommand_from head" -s n -l lines -r -d 'First N lines (default 10)'
complete -c live -n "__fish_seen_subcommand_from head" -s c -l bytes -r -d 'First K bytes'

# tail
complete -c live -n "__fish_seen_subcommand_from tail" -s v -l verbose
complete -c live -n "__fish_seen_subcommand_from tail" -s f -l follow -d 'Follow new lines'
complete -c live -n "__fish_seen_subcommand_from tail" -s g -l global -d 'Resolve selector globally'
complete -c live -n "__fish_seen_subcommand_from tail" -l strip-ansi
complete -c live -n "__fish_seen_subcommand_from tail" -l raw
complete -c live -n "__fish_seen_subcommand_from tail" -s n -l lines -r -d 'Last N lines'
complete -c live -n "__fish_seen_subcommand_from tail" -s c -l bytes -r -d 'Last K bytes'
complete -c live -n "__fish_seen_subcommand_from tail" -l since -r -d 'Time cursor (epoch seconds)'

# rm
complete -c live -n "__fish_seen_subcommand_from rm" -s f -l force -d 'Kill running recorders'
complete -c live -n "__fish_seen_subcommand_from rm" -s g -l global -d 'Resolve selectors globally'
complete -c live -n "__fish_seen_subcommand_from rm" -l all-exited -d 'Remove every dead session'

# run -- hand off after first non-flag token.
complete -c live -n "__fish_seen_subcommand_from run" -s n -r -d 'Session name'
complete -c live -n "__fish_seen_subcommand_from run; and __fish_complete_subcommand --skip 2" \
    -a "(__fish_complete_subcommand --skip 2)"

# completion
complete -c live -n "__fish_seen_subcommand_from completion" -a "bash zsh fish"
"""


def script_for(shell: str) -> str | None:
    return {"bash": BASH, "zsh": ZSH, "fish": FISH}.get(shell)
