"""Shell completion script payloads, returned by `live completion <shell>`.

Each script offers verb completion, per-verb flag completion, session-name
completion (via `live ls -a --json`), and `live run <TAB>` handoff to the
wrapped command's completion.
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
        COMPREPLY=( $(compgen -W "run ls cat tail rm llms.txt completion" -- "$cur") )
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
                    -n|--name) i=$((i+1)) ;;
                    --name=*) ;;
                    -*) ;;
                    *) seen_cmd=$i; break ;;
                esac
            done
            if [ "$seen_cmd" -gt 0 ] && type _command_offset >/dev/null 2>&1; then
                _command_offset $seen_cmd
                return
            fi
            COMPREPLY=( $(compgen -W "-n --name --" -- "$cur") )
            ;;
        ls)
            COMPREPLY=( $(compgen -W "-a --all -g --global -n --name --json" -- "$cur") )
            ;;
        cat)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -g --global --strip-ansi --raw" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_session_names)" -- "$cur") )
            fi
            ;;
        tail)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -f --follow -g --global --strip-ansi --raw -n --lines -c --bytes --since-line" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_session_names)" -- "$cur") )
            fi
            ;;
        rm)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-f --force -g --global --all-exited" -- "$cur") )
            else
                COMPREPLY=( $(compgen -W "$(_live_session_names)" -- "$cur") )
            fi
            ;;
        completion)
            COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
            ;;
    esac
}

_live_session_names() {
    live ls -a --json 2>/dev/null \
        | sed -nE 's/.*"name":"([^"]+)".*/\1/p' \
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
                    # Complete our flags before the wrapped command; any non-flag
                    # word triggers `_normal` against the wrapped command.
                    _arguments -S \
                        '(-n --name)'{-n+,--name=}':name:' \
                        '*::command:_normal'
                    ;;
                ls)
                    _arguments \
                        '(-a --all)'{-a,--all} \
                        '(-g --global)'{-g,--global} \
                        '(-n --name)'{-n+,--name=}':name:' \
                        '--json'
                    ;;
                cat)
                    _arguments \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-g --global)'{-g,--global} \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '1:selector:_live_sessions'
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
                        '--since-line=:cursor:' \
                        '1:selector:_live_sessions'
                    ;;
                rm)
                    _arguments \
                        '(-f --force)'{-f,--force} \
                        '(-g --global)'{-g,--global} \
                        '--all-exited' \
                        '*:selector:_live_sessions'
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
        'tail:Tail a session'
        'rm:Delete sessions'
        'llms.txt:Print a token-minimal agent guide'
        'completion:Print shell completion script'
    )
    _describe -t verbs 'verb' verbs
}

_live_sessions() {
    local -a names
    names=( ${(f)"$(live ls -a --json 2>/dev/null | sed -nE 's/.*"name":"([^"]+)".*/\1/p' | sort -u)"} )
    (( $#names )) && _values 'session' "${names[@]}"
}

_live "$@"
"""


FISH = r"""# fish completion for live

function __live_session_names
    live ls -a --json 2>/dev/null | string match -rg '"name":"([^"]+)"' | sort -u
end

set -l verbs run ls cat tail rm llms.txt completion

complete -c live -f
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a run -d 'Wrap <cmd> under a PTY'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a ls -d 'List sessions'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a cat -d 'Concatenate stream.*.log'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a tail -d 'Tail a session'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a rm -d 'Delete sessions'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a llms.txt -d 'Print agent guide'
complete -c live -n "not __fish_seen_subcommand_from $verbs" -a completion -d 'Print completion script'

# Selector completion for cat / tail / rm.
complete -c live -n "__fish_seen_subcommand_from cat tail rm" -a "(__live_session_names)"

# ls
complete -c live -n "__fish_seen_subcommand_from ls" -s a -l all -d 'Include exited sessions'
complete -c live -n "__fish_seen_subcommand_from ls" -s g -l global -d 'Show sessions from all directories'
complete -c live -n "__fish_seen_subcommand_from ls" -l json -d 'Emit NDJSON'
complete -c live -n "__fish_seen_subcommand_from ls" -s n -l name -r -d 'Filter to NAME'

# cat
complete -c live -n "__fish_seen_subcommand_from cat" -s v -l verbose -d 'Add stderr metadata'
complete -c live -n "__fish_seen_subcommand_from cat" -s g -l global -d 'Resolve selector globally'
complete -c live -n "__fish_seen_subcommand_from cat" -l strip-ansi -d 'Remove ANSI escapes'
complete -c live -n "__fish_seen_subcommand_from cat" -l raw -d 'Keep ANSI escapes'

# tail
complete -c live -n "__fish_seen_subcommand_from tail" -s v -l verbose
complete -c live -n "__fish_seen_subcommand_from tail" -s f -l follow -d 'Follow new lines'
complete -c live -n "__fish_seen_subcommand_from tail" -s g -l global -d 'Resolve selector globally'
complete -c live -n "__fish_seen_subcommand_from tail" -l strip-ansi
complete -c live -n "__fish_seen_subcommand_from tail" -l raw
complete -c live -n "__fish_seen_subcommand_from tail" -s n -l lines -r -d 'Last N lines'
complete -c live -n "__fish_seen_subcommand_from tail" -s c -l bytes -r -d 'Last K bytes'
complete -c live -n "__fish_seen_subcommand_from tail" -l since-line -r -d 'Resumable cursor'

# rm
complete -c live -n "__fish_seen_subcommand_from rm" -s f -l force -d 'Kill running recorders'
complete -c live -n "__fish_seen_subcommand_from rm" -s g -l global -d 'Resolve selectors globally'
complete -c live -n "__fish_seen_subcommand_from rm" -l all-exited -d 'Remove every dead session'

# run -- hand off after first non-flag token.
complete -c live -n "__fish_seen_subcommand_from run" -s n -l name -r -d 'Session name'
complete -c live -n "__fish_seen_subcommand_from run; and __fish_complete_subcommand --skip 2" \
    -a "(__fish_complete_subcommand --skip 2)"

# completion
complete -c live -n "__fish_seen_subcommand_from completion" -a "bash zsh fish"
"""


def script_for(shell: str) -> str | None:
    return {"bash": BASH, "zsh": ZSH, "fish": FISH}.get(shell)
