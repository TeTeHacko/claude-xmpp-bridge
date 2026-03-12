# Bubblewrap Sandbox

A secure execution environment for AI coding tools (Claude Code, OpenCode) using `bwrap` (Bubblewrap).

This wrapper script restricts the AI's filesystem access to prevent it from accidentally or maliciously reading sensitive files in your home directory or modifying system files, while still allowing it to function normally within the project directory.

## Features

- **Filesystem Isolation:** Restricts write access to the current working directory (`CWD`) and `/tmp`.
- **Home Directory Protection:** Completely blocks access to `$HOME` (e.g., `~/.ssh`, `~/.aws`, `~/.kube`, `~/.gnupg`).
- **Targeted Tool Access:** Explicitly allows access to required tool directories (`~/.claude`, `~/.config/opencode`, `~/.config/claude-xmpp-bridge`).
- **Read-Only System:** Mounts system directories (`/usr`, `/lib`, `/etc`) as read-only.
- **Dynamic Credential Injection:** Selectively mount specific SSH keys or Kubernetes contexts if needed.
- **Network Access:** Network is preserved so tools can reach APIs.

## Requirements

- `bwrap` (Bubblewrap) - `sudo apt install bubblewrap` or `sudo pacman -S bubblewrap`

## Installation

The `claude-xmpp-bridge-setup` wizard can automatically install this script to `~/.local/bin/sandbox` (Step 7).

Or install manually:
```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/sandbox" ~/.local/bin/sandbox

# Bash completion (optional)
mkdir -p ~/.local/share/bash-completion/completions
ln -sf "$(pwd)/sandbox.bash-completion" ~/.local/share/bash-completion/completions/sandbox
```

Using symlinks means `pipx upgrade` automatically propagates sandbox updates.

## Usage

Prefix your AI tool execution with `sandbox`:

```bash
sandbox claude
sandbox opencode
```

### Options

```text
Usage: sandbox [OPTIONS] COMMAND [ARGS...]

Options:
  -c "cmd"       Run command via bash -c
  -k KEY         SSH key name (repeatable), or "list"
  -K CONTEXT     Kubernetes context (repeatable), or "list"
  -w PATH        Extra RW bind mount (repeatable)
  -r PATH        Extra RO bind mount (repeatable)
  -h             Show this help
```

### Examples

**Run OpenCode:**
```bash
sandbox opencode
```

**Run Claude Code and mount a specific SSH key:**
```bash
sandbox -k id_ed25519 claude
```

**Run Claude Code and mount a specific Kubernetes context:**
```bash
sandbox -K prod-cluster claude
```

**Run with extra read-write mounts:**
```bash
sandbox -w /var/log/my-app opencode
```

## Bash Completion

Tab completion is installed automatically by `claude-xmpp-bridge-setup` (Step 7) to `~/.local/share/bash-completion/completions/sandbox`. It completes:

- Options (`-c`, `-k`, `-K`, `-w`, `-r`, `-h`)
- SSH key names from `~/.ssh/` (for `-k`)
- Kubernetes contexts from `kubectl` (for `-K`)
- Filesystem paths (for `-w` and `-r`)
- Commands from `$PATH` (for the sandboxed command)
