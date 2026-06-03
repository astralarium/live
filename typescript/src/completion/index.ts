/**
 * Completion-script generation for bash / zsh / fish. Before any non-flag
 * arg or `--`, complete `live`'s own flags; after, defer to the wrapped
 * command's completion as if `live` weren't there.
 */

export type Shell = "bash" | "zsh" | "fish";

export function isShell(value: string): value is Shell {
  return value === "bash" || value === "zsh" || value === "fish";
}

export function completionScript(shell: Shell): string {
  switch (shell) {
    case "bash":
      return BASH;
    case "zsh":
      return ZSH;
    case "fish":
      return FISH;
  }
}

const BASH = `# bash completion for \`live\`. Source from ~/.local/share/bash-completion/completions/live
# Requires the bash-completion package (provides _init_completion and _command_offset).
_live() {
  local cur prev words cword
  _init_completion || return

  # If we're completing the argument to \`--completion\`, offer shell names.
  if [[ "${"$"}prev" == "--completion" ]]; then
    COMPREPLY=( $(compgen -W "bash zsh fish" -- "${"$"}cur") )
    return
  fi
  # --name/-n takes a free-form label; nothing to suggest.
  if [[ "${"$"}prev" == "--name" || "${"$"}prev" == "-n" ]]; then
    return
  fi

  # Find first non-flag argument (the wrapped command), or the bare \`--\` separator.
  # --name/-n consume one extra token (their value).
  local i cmd_start=0
  for (( i=1; i<${"$"}{#COMP_WORDS[@]}; i++ )); do
    local w="${"$"}{COMP_WORDS[i]}"
    if [[ "${"$"}w" == "--" ]]; then
      cmd_start=$((i+1))
      break
    fi
    if [[ "${"$"}w" == "--name" || "${"$"}w" == "-n" ]]; then
      i=$((i+1))
      continue
    fi
    if [[ "${"$"}w" != -* ]]; then
      cmd_start=$i
      break
    fi
  done

  if (( cmd_start == 0 )); then
    # No command yet — completing a token that's still in live's own flag space.
    if [[ "${"$"}cur" == -* ]]; then
      COMPREPLY=( $(compgen -W "--init --mcp --name -n --completion --help -h --" -- "${"$"}cur") )
    fi
    return
  fi

  # Defer to the wrapped command's completion as if \`live\` weren't there.
  _command_offset $cmd_start
}
complete -F _live live
`;

const ZSH = `# zsh completion for \`live\`. Drop in $fpath as _live
#compdef live

_live() {
  # If completing the argument to \`--completion\`, offer shell names.
  if (( CURRENT >= 2 )) && [[ "${"$"}words[CURRENT-1]" == "--completion" ]]; then
    _values 'shell' bash zsh fish
    return
  fi
  # --name/-n takes a free-form label; nothing to suggest.
  if (( CURRENT >= 2 )) && [[ "${"$"}words[CURRENT-1]" == "--name" || "${"$"}words[CURRENT-1]" == "-n" ]]; then
    return
  fi

  # Find first non-flag arg (the wrapped command), or \`--\`.
  # --name/-n consume one extra token (their value).
  local i cmd_start=0
  for (( i=2; i<=${"$"}#words; i++ )); do
    if [[ "${"$"}words[i]" == "--" ]]; then
      cmd_start=$((i+1))
      break
    fi
    if [[ "${"$"}words[i]" == "--name" || "${"$"}words[i]" == "-n" ]]; then
      i=$((i+1))
      continue
    fi
    if [[ "${"$"}words[i]" != -* ]]; then
      cmd_start=$i
      break
    fi
  done

  if (( cmd_start == 0 )); then
    # No command yet — offer live's own flags.
    _values 'live flag' \\
      '--init[create .live/ in cwd]' \\
      '--mcp[start MCP server]' \\
      '--name[label this session]:name:' \\
      '-n[label this session]:name:' \\
      '--completion[print shell completion script]:shell:(bash zsh fish)' \\
      '--help[show usage]' \\
      '-h[show usage]' \\
      '--[end of live options]'
    return
  fi

  # Defer to the wrapped command's completion.
  shift $((cmd_start - 1)) words
  (( CURRENT -= (cmd_start - 1) ))
  _normal
}
_live
`;

const FISH = `# fish completion for \`live\`. Save to ~/.config/fish/completions/live.fish

# Suppress live's default flag list once a command is being entered.
# --name/-n consume one extra token (their value).
function __live_no_cmd_yet
  set -l tokens (commandline -opc)
  set -l skip_next 0
  for i in (seq 2 (count $tokens))
    set -l t $tokens[$i]
    if test $skip_next -eq 1
      set skip_next 0
      continue
    end
    if test "$t" = "--"
      return 1
    end
    if test "$t" = "--name" -o "$t" = "-n"
      set skip_next 1
      continue
    end
    if not string match -q -- "-*" $t
      return 1
    end
  end
  return 0
end

complete -c live -n __live_no_cmd_yet -l init -d "create .live/ in cwd"
complete -c live -n __live_no_cmd_yet -l mcp -d "start MCP server"
complete -c live -n __live_no_cmd_yet -l name -r -d "label this session"
complete -c live -n __live_no_cmd_yet -s n -r -d "label this session"
complete -c live -n __live_no_cmd_yet -l completion -r -d "print shell completion script" \\
  -a "bash zsh fish"

# After live's own options, defer to the wrapped command's completion.
complete -c live -n "not __live_no_cmd_yet" -x -a "(__fish_complete_subcommand)"
`;
