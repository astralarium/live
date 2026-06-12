"""Shell completion script payloads, returned by `live completion-script <shell>`.

Each script offers verb completion, per-verb flag completion, selector
completion, and `live run <TAB>` handoff to the wrapped command's completion.
Candidates come from the plumbing verbs `live completion selectors` (names +
ids, scoped like `ls`) and `live completion cwds` (session cwds), one per
line — no output parsing in the scripts. The selector helper mirrors the
verb's scope flags: `ls` only suggests active sessions unless `-a` was typed;
the read verbs (`cat`/`head`/`tail`/`less`/`rm`) always pass `-a` since exited
sessions remain valid targets; `stop` only suggests active sessions. `-g`
and `-C <dir>` are honored when present in the command line. `-C` values
complete from the cwds of recorded sessions; plain directory completion is
the fallback when no recorded cwd matches the typed prefix, so the session
dirs aren't drowned out (and keep a useful common prefix).
"""

from __future__ import annotations


BASH = r"""# bash completion for live
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
            COMPREPLY=( $(compgen -W "--version" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "run ls cat head tail less stop rm llms.txt completion completion-script update-shell" -- "$cur") )
        fi
        return
    fi

    # Complete a `-C/--cwd` value, except inside `run`'s wrapped command
    # (the handoff below owns that).
    if [ "$verb" != run ] && _live_prev_is_cwd; then
        _live_complete_cwd; return
    fi

    case "$verb" in
        run)
            # If the user has typed a non-flag arg after `run`, hand off to the wrapped
            # command's completion via _command_offset.
            local seen_cmd=0
            for ((i=verb_idx+1; i<cword; i++)); do
                case "${words[i]}" in
                    --) seen_cmd=$((i+1)); break ;;
                    # Value-taking flags, exact or cluster-final (`-dn NAME`).
                    -n|--name|-C|--cwd|--geometry|-[!-nC]*[nC])
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
            COMPREPLY=( $(compgen -W "-n --name -d --detach -C --cwd --geometry --" -- "$cur") )
            ;;
        ls)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-a --all -g --global -C --cwd --json" -- "$cur") )
            else
                _live_complete_selectors $(_live_all_flag)
            fi
            ;;
        cat)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -g --global -C --cwd --strip-ansi --raw" -- "$cur") )
            else
                _live_complete_selectors -a
            fi
            ;;
        less)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-g --global -C --cwd --strip-ansi --raw" -- "$cur") )
            else
                _live_complete_selectors -a
            fi
            ;;
        head)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -g --global -C --cwd --strip-ansi --raw -n --lines -c --bytes -t --time" -- "$cur") )
            elif ! _live_prev_is_count; then
                _live_complete_selectors -a
            fi
            ;;
        tail)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-v --verbose -f --follow -g --global -C --cwd --strip-ansi --raw -n --lines -c --bytes -t --time" -- "$cur") )
            elif ! _live_prev_is_count; then
                _live_complete_selectors -a
            fi
            ;;
        stop)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-g --global -C --cwd --all" -- "$cur") )
            else
                _live_complete_selectors
            fi
            ;;
        rm)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=( $(compgen -W "-f --force -g --global -C --cwd --all --exited --untitled --older-than" -- "$cur") )
            elif ! _live_prev_is_count; then
                _live_complete_selectors -a
            fi
            ;;
        completion)
            COMPREPLY=( $(compgen -W "selectors cwds" -- "$cur") )
            ;;
        completion-script|update-shell)
            COMPREPLY=( $(compgen -W "bash zsh fish" -- "$cur") )
            ;;
    esac
}

# True when the cursor sits on a `-C/--cwd` value. Readline splits
# `--cwd=PATH` on `=`, so a `=` prev means look one word further back.
_live_prev_is_cwd() {
    case "$prev" in
        -C|--cwd) return 0 ;;
        =) case "${words[cword-2]}" in -C|--cwd) return 0 ;; esac ;;
    esac
    return 1
}

# True when the cursor sits on a count/duration value (`-n/-c/-t`, long
# forms including `--older-than`, or cluster-final like `-fn`); selectors
# must not be offered there.
_live_prev_is_count() {
    case "$prev" in
        -[nct]|--lines|--bytes|--time|--older-than|-[!-nctC]*[nct]) return 0 ;;
        =) case "${words[cword-2]}" in -[nct]|--lines|--bytes|--time|--older-than) return 0 ;; esac ;;
    esac
    return 1
}

# Prints `-a` when --all, `-a`, or a short cluster containing `a` (`-ag`)
# was typed; `a` inside an attached `-C` value doesn't count.
_live_all_flag() {
    local i
    for ((i=1; i<cword; i++)); do
        case "${words[i]}" in
            --all) echo -a; return ;;
            --*) ;;
            -*) case "${words[i]%%C*}" in *a*) echo -a; return ;; esac ;;
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

# Selector completion; arguments (`-a`) plus any typed scope flag
# (`-g`, or `-C <dir>`, clustered or attached) are forwarded to
# `live completion selectors`.
_live_complete_selectors() {
    local -a sel_args=("$@")
    local i j w cluster
    for ((i=1; i<cword; i++)); do
        w="${words[i]}"
        case "$w" in
            --global) sel_args+=(-g) ;;
            -C|--cwd)
                j=$((i+1))
                [ "${words[j]}" = "=" ] && j=$((j+1))
                (( j < cword )) && sel_args+=(-C "${words[j]}")
                ;;
            --*) ;;
            -?*)
                cluster="${w%%C*}"  # short flags before any -C value
                case "$cluster" in *g*) sel_args+=(-g) ;; esac
                if [ "$w" != "$cluster" ]; then
                    if [ -n "${w##*C}" ]; then
                        sel_args+=(-C "${w#*C}")  # attached: -CPATH
                    else
                        j=$((i+1))  # cluster-final: -aC PATH
                        [ "${words[j]}" = "=" ] && j=$((j+1))
                        (( j < cword )) && sel_args+=(-C "${words[j]}")
                    fi
                fi
                ;;
        esac
    done
    COMPREPLY=()
    _live_reply_lines < <(live completion selectors "${sel_args[@]}" 2>/dev/null)
}

# `-C` value completion: cwds of recorded sessions; plain directories only
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


ZSH = r"""#compdef live

_live() {
    local context state line
    typeset -A opt_args

    _arguments -C \
        '--version[Show version and exit]' \
        '1: :_live_verbs' \
        '*::arg:->args' \
        && return 0

    case $state in
        args)
            case $words[1] in
                run)
                    # Complete our flag before the wrapped command; any non-flag
                    # word triggers `_normal` against the wrapped command.
                    # `-s` lets clustered flags (`-dn NAME`) parse correctly.
                    _arguments -s -S \
                        {-n+,--name=}'[session name]:name:' \
                        '(-d --detach)'{-d,--detach}'[detach; print session id]' \
                        '(-C --cwd)'{-C+,--cwd=}'[working directory]:directory:_live_cwds' \
                        '--geometry=[PTY size]:COLSxROWS:' \
                        '*::command:_normal'
                    ;;
                ls)
                    _arguments -s \
                        '(-a --all)'{-a,--all} \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '--json' \
                        '1:selector:_live_selectors'
                    ;;
                cat)
                    _arguments -s \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '1:selector:_live_selectors'
                    ;;
                less)
                    _arguments -s \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '1:selector:_live_selectors'
                    ;;
                head)
                    _arguments -s \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '(-n --lines)'{-n+,--lines=}':lines:' \
                        '(-c --bytes)'{-c+,--bytes=}':bytes:' \
                        '(-t --time)'{-t+,--time=}':time (epoch, duration, or ISO):' \
                        '1:selector:_live_selectors'
                    ;;
                tail)
                    _arguments -s \
                        '(-v --verbose)'{-v,--verbose} \
                        '(-f --follow)'{-f,--follow} \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '(--strip-ansi --raw)--strip-ansi' \
                        '(--strip-ansi --raw)--raw' \
                        '(-n --lines)'{-n+,--lines=}':lines:' \
                        '(-c --bytes)'{-c+,--bytes=}':bytes:' \
                        '(-t --time)'{-t+,--time=}':time (epoch, duration, or ISO):' \
                        '1:selector:_live_selectors'
                    ;;
                stop)
                    _arguments -s \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '--all' \
                        '*:selector:_live_selectors'
                    ;;
                rm)
                    _arguments -s \
                        '(-f --force)'{-f,--force} \
                        '(-g --global -C --cwd)'{-g,--global} \
                        '(-C --cwd -g --global)'{-C+,--cwd=}'[directory scope]:directory:_live_cwds' \
                        '--all' \
                        '--exited' \
                        '--untitled' \
                        '--older-than=:age (e.g. 7d, 12h):' \
                        '*:selector:_live_selectors'
                    ;;
                completion)
                    _arguments '1:what:(selectors cwds)'
                    ;;
                completion-script|update-shell)
                    _arguments '1:shell:(bash zsh fish)'
                    ;;
            esac
            ;;
    esac
}

_live_verbs() {
    local -a verbs
    verbs=(
        'run:Run <cmd> under a PTY; record.'
        'ls:List sessions.'
        'cat:Concatenate session.'
        'head:Head session.'
        'tail:Tail session.'
        'less:Page session.'
        'stop:Stop running sessions.'
        'rm:Delete sessions.'
        'llms.txt:Print agent instructions.'
        'completion:Print completion candidates.'
        'completion-script:Print shell completion script.'
        'update-shell:Install shell completions.'
    )
    _describe -t verbs 'verb' verbs
}

# Selector completion. For `live ls`, only suggest active sessions unless -a
# was typed; `stop` only suggests active sessions; other verbs always include
# exited (still valid targets). Honors -g/--global and -C/--cwd when present
# in the command line, clustered (`-ag`, `-aC <dir>`) or attached (`-C<dir>`).
_live_selectors() {
    local -a sel_args names
    local i cluster want_all=0 want_global=0
    for (( i=2; i <= $#words; i++ )); do
        case $words[i] in
            -a|--all) want_all=1 ;;
            -g|--global) want_global=1 ;;
            -C|--cwd) (( i < $#words )) && sel_args+=(-C "$words[i+1]") ;;
            --cwd=*) sel_args+=(-C "${words[i]#--cwd=}") ;;
            -C*) sel_args+=(-C "${words[i]#-C}") ;;
            --*) ;;
            -*)
                cluster="${words[i]%%C*}"  # short flags before any -C value
                [[ $cluster == *a* ]] && want_all=1
                [[ $cluster == *g* ]] && want_global=1
                if [[ $words[i] != "$cluster" ]]; then
                    if [[ -n ${words[i]##*C} ]]; then
                        sel_args+=(-C "${words[i]#*C}")  # attached: -aC<dir>
                    else
                        (( i < $#words )) && sel_args+=(-C "$words[i+1]")
                    fi
                fi
                ;;
        esac
    done
    case $words[1] in
        stop) ;;
        ls) (( want_all )) && sel_args+=(-a) ;;
        *) sel_args+=(-a) ;;
    esac
    (( want_global )) && sel_args+=(-g)
    names=( ${(f)"$(live completion selectors $sel_args 2>/dev/null)"} )
    (( $#names )) && _values 'selector' "${names[@]}"
}

# `-C` value completion: cwds of recorded sessions; plain directories only
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


FISH = r"""# fish completion for live

# True when a short-flag cluster contains the letter $argv[1] (`-ag`);
# letters inside an attached `-C` value don't count.
function __live_cluster_has
    for t in $argv[2..-1]
        string match -qr -- "^-[^-C]*$argv[1]" $t; and return 0
    end
    return 1
end

function __live_selectors
    set -l toks (commandline -opc)
    set -l verb $toks[2]
    set -l args
    if test "$verb" = "stop"
        # active sessions only
    else if test "$verb" != "ls"; or contains -- --all $toks; or __live_cluster_has a $toks
        set -a args -a
    end
    if contains -- --global $toks; or __live_cluster_has g $toks
        set -a args -g
    end
    # -C value: next token (exact or cluster-final), --cwd=DIR, or attached -CDIR.
    set -l n (count $toks)
    set -l i 2
    while test $i -le $n
        set -l t $toks[$i]
        if test "$t" = --cwd; or string match -qr -- '^-[^-]*C$' $t
            test $i -lt $n; and set -a args -C $toks[(math $i + 1)]
        else if string match -qr -- '^--cwd=.' $t
            set -a args -C (string replace -- --cwd= '' $t)
        else if string match -qr -- '^-C.' $t
            set -a args -C (string sub -s 3 -- $t)
        end
        set i (math $i + 1)
    end
    live completion selectors $args 2>/dev/null
end

# `-C` value completion: cwds of recorded sessions; plain directories only
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

# True while the cursor is still on `run`'s own flags — before the wrapped
# command's first token.
function __live_run_needs_flags
    set -l toks (commandline -opc)
    set -l i 3
    while test $i -le (count $toks)
        switch $toks[$i]
            case --
                return 1
            case -n --name -C --cwd --geometry
                set i (math $i + 2)
            case '-*'
                # A cluster ending in a value-taking flag skips its value too.
                if string match -qr -- '^-[^-nC][^-]*[nC]$' $toks[$i]
                    set i (math $i + 2)
                else
                    set i (math $i + 1)
                end
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

set -l verbs run ls cat head tail less stop rm llms.txt completion completion-script update-shell

complete -c live -f
complete -c live -n "not __live_verb_is $verbs" -a run -d 'Run <cmd> under a PTY; record.'
complete -c live -n "not __live_verb_is $verbs" -a ls -d 'List sessions.'
complete -c live -n "not __live_verb_is $verbs" -a cat -d 'Concatenate session.'
complete -c live -n "not __live_verb_is $verbs" -a head -d 'Head session.'
complete -c live -n "not __live_verb_is $verbs" -a tail -d 'Tail session.'
complete -c live -n "not __live_verb_is $verbs" -a less -d 'Page session.'
complete -c live -n "not __live_verb_is $verbs" -a stop -d 'Stop running sessions.'
complete -c live -n "not __live_verb_is $verbs" -a rm -d 'Delete sessions.'
complete -c live -n "not __live_verb_is $verbs" -a llms.txt -d 'Print agent instructions.'
complete -c live -n "not __live_verb_is $verbs" -a completion -d 'Print completion candidates.'
complete -c live -n "not __live_verb_is $verbs" -a completion-script -d 'Print shell completion script.'
complete -c live -n "not __live_verb_is $verbs" -a update-shell -d 'Install shell completions.'
complete -c live -n "not __live_verb_is $verbs" -l version -d 'Show version and exit.'

# Selector completion for ls / cat / head / tail / less / stop / rm.
complete -c live -n "__live_verb_is ls cat head tail less stop rm" -a "(__live_selectors)"

# ls
complete -c live -n "__live_verb_is ls" -s a -l all -d 'Include exited.'
complete -c live -n "__live_verb_is ls" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is ls" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is ls" -l json -d 'Emit NDJSON.'

# cat
complete -c live -n "__live_verb_is cat" -s v -l verbose -d 'Verbose output.'
complete -c live -n "__live_verb_is cat" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is cat" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is cat" -l strip-ansi -d 'Strip ANSI.'
complete -c live -n "__live_verb_is cat" -l raw -d 'Keep ANSI.'

# less
complete -c live -n "__live_verb_is less" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is less" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is less" -l strip-ansi -d 'Strip ANSI.'
complete -c live -n "__live_verb_is less" -l raw -d 'Keep ANSI.'

# head
complete -c live -n "__live_verb_is head" -s v -l verbose -d 'Verbose output.'
complete -c live -n "__live_verb_is head" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is head" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is head" -l strip-ansi -d 'Strip ANSI.'
complete -c live -n "__live_verb_is head" -l raw -d 'Keep ANSI.'
complete -c live -n "__live_verb_is head" -s n -l lines -r -d 'First N lines (default 10); -N drops last N.'
complete -c live -n "__live_verb_is head" -s c -l bytes -r -d 'First K bytes; -K drops last K.'
complete -c live -n "__live_verb_is head" -s t -l time -r -d 'Lines with idx t <= T (epoch).'

# tail
complete -c live -n "__live_verb_is tail" -s v -l verbose -d 'Verbose output.'
complete -c live -n "__live_verb_is tail" -s f -l follow -d 'Follow until exit.'
complete -c live -n "__live_verb_is tail" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is tail" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is tail" -l strip-ansi -d 'Strip ANSI.'
complete -c live -n "__live_verb_is tail" -l raw -d 'Keep ANSI.'
complete -c live -n "__live_verb_is tail" -s n -l lines -r -d 'Last N lines (default 10); +N for lines n >= N.'
complete -c live -n "__live_verb_is tail" -s c -l bytes -r -d 'Last K bytes; +K for bytes after offset K.'
complete -c live -n "__live_verb_is tail" -s t -l time -r -d 'Lines with idx t > T (epoch).'

# stop
complete -c live -n "__live_verb_is stop" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is stop" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is stop" -l all -d 'Stop all running sessions.'

# rm
complete -c live -n "__live_verb_is rm" -s f -l force -d 'SIGTERM live runs; ignore missing.'
complete -c live -n "__live_verb_is rm" -s g -l global -d 'Global scope.'
complete -c live -n "__live_verb_is rm" -s C -l cwd -x -a "(__live_cwds)" -d 'Directory scope.'
complete -c live -n "__live_verb_is rm" -l all -d 'Delete all sessions.'
complete -c live -n "__live_verb_is rm" -l exited -d 'Delete exited sessions.'
complete -c live -n "__live_verb_is rm" -l untitled -d 'Delete unnamed sessions.'
complete -c live -n "__live_verb_is rm" -l older-than -r -d 'Delete sessions exited before AGE: duration (7d, 12h, 30m, 60s) or ISO datetime.'

# run -- own flags before the wrapped command; hand off afterwards.
complete -c live -n "__live_verb_is run; and __live_run_needs_flags" -s n -l name -r -d 'Session name.'
complete -c live -n "__live_verb_is run; and __live_run_needs_flags" -s d -l detach -d 'Detach; print session id.'
complete -c live -n "__live_verb_is run; and __live_run_needs_flags" -s C -l cwd -x -a "(__live_cwds)" -d 'Working directory.'
complete -c live -n "__live_verb_is run; and __live_run_needs_flags" -l geometry -r -d 'PTY size as COLSxROWS.'
# Trailing args are value-taking flags to skip; the helper matches tokens
# exactly, so clustered forms (`-dn`, `-dC`) must be listed too.
complete -c live -n "__live_verb_is run" \
    -a "(__fish_complete_subcommand --fcs-skip=2 -n --name -C --cwd --geometry -dn -dC)"

# completion / completion-script / update-shell
complete -c live -n "__live_verb_is completion" -a "selectors cwds"
complete -c live -n "__live_verb_is completion-script update-shell" -a "bash zsh fish"
"""


def script_for(shell: str) -> str | None:
    return {"bash": BASH, "zsh": ZSH, "fish": FISH}.get(shell)
