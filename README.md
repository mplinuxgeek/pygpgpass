# pygpgpass

A minimal, zero-dependency, pure Python alternative to `gopass` and `pass`.

Built for locked-down or restricted environments (corporate Windows machines without admin rights, WSL, or any system where only Python and Git Bash are available). Interacts directly with the GPG binary already bundled with Git for Windows — no pip, no virtualenv, no admin.

---

## Features

- **Full command set:** `init`, `list`, `find`, `show`, `copy`, `insert`, `edit`, `rename`/`mv`, `rm`, `git`, `sync`
- **ASCII tree view:** Recursively lists secrets, hiding empty directories and non-password files
- **Cross-platform clipboard:** Supports `clip.exe` (Windows), `pbcopy` (macOS), `wl-copy` (Wayland), `xclip`, and `xsel` (X11) — auto-detected
- **Clipboard auto-clear:** Copied password is cleared after 90 seconds, only if the clipboard hasn't changed
- **Pronounceable password generator:** Cryptographically secure via Python's `secrets` module; opens in editor for review before saving
- **Atomic writes:** Encryption uses a temp file + atomic rename — a failed write never corrupts an existing secret
- **Secure temp files:** Editor temp files are written to RAM (`/dev/shm`) on Linux where available, zero-wiped before deletion
- **Path safety:** Secret names are bounds-checked against the store root — no directory traversal

---

## Prerequisites

1. **Python 3.6+**
2. **Git for Windows** (provides Git Bash and the bundled `gpg` binary)
3. A GPG key pair — run `gpg --gen-key` if you don't have one

> [!TIP]
> For the best experience on Windows, run Git Bash inside **Windows Terminal** rather than the legacy `mintty` console.

---

## Installation & Setup

### 1. Download the script

Save `pygpgpass.py` to your home directory:

```bash
# inside Git Bash
cp pygpgpass.py ~/pygpgpass.py
```

### 2. Add a shell alias

Open `~/.bashrc` and add:

```bash
alias gopass="python3 ~/pygpgpass.py"
```

Reload your profile:

```bash
source ~/.bashrc
```

### 3. Initialise the password store

```bash
gopass init
```

You will be prompted for a storage path. The default is `~/gpg`. The path is saved to `~/.pygpgpassrc` (mode `600`).

---

## Usage

```
gopass init [--force]
gopass list
gopass find <term>
gopass show <name> [<line>|<field>]
gopass copy <name>
gopass insert <name> [--random]
gopass edit <name>
gopass cp <src> <dst>
gopass rename <old> <new>
gopass mv <old> <new>
gopass rm <name>
gopass git <subcommand>
gopass sync
```

### `init`

Initialises the password store and writes `~/.pygpgpassrc`. Aborts if already initialised to protect an existing working install.

```bash
gopass init
```

Use `--force` to reconfigure an existing install:

```bash
gopass init --force
```

### `list`

Prints an ASCII tree of all secrets in the store.

```bash
gopass list
```

```
pygpgpass (/home/user/gpg)
├── personal
│   ├── email
│   └── github
└── work
    └── vpn
```

### `find`

Searches secret names by substring (case-insensitive).

```bash
gopass find git
# personal/github
# work/gitlab
```

### `show`

Decrypts and prints a secret to stdout.

```bash
gopass show work/vpn
```

Print a specific line by number (1-indexed):

```bash
gopass show work/vpn 2
```

Extract a named field — matches lines of the form `key: value`:

```bash
gopass show work/vpn username
gopass show work/vpn url
```

### `copy`

Decrypts a secret and copies the **first non-blank line** to the clipboard. Clipboard is automatically cleared after 90 seconds if it hasn't been overwritten.

```bash
gopass copy work/vpn
# [Copied 'work/vpn' to clipboard — clears in 90s]
```

### `insert`

Stores a new secret. Type your content line by line, then type `SAVE` on its own line to encrypt and save.

```bash
gopass insert personal/email
```

Use `--random` to generate a cryptographically secure, pronounceable 15-character password and review it in your editor before saving:

```bash
gopass insert personal/email --random
```

### `edit`

Opens an existing secret in your `$EDITOR` for modification. If the secret does not exist it is created. Changes are encrypted on save; no changes aborts without writing.

```bash
gopass edit personal/email
```

### `cp`

Copies a secret to a new path without decrypting — the `.gpg` file is duplicated as-is. Use this when you want a base template to then edit independently.

```bash
gopass cp work/vpn work/vpn-backup
# then edit one independently:
gopass edit work/vpn
```

### `rename` / `mv`

Moves a secret to a new path. Empty parent directories left behind are pruned automatically. `mv` is an alias.

```bash
gopass rename personal/email personal/gmail
gopass mv personal/email personal/gmail
```

### `rm`

Deletes a secret after confirmation. Empty parent directories are pruned.

```bash
gopass rm personal/email
# Are you sure you want to delete 'personal/email'? [y/N]:
```

### `git`

Runs any git command inside the password store directory. Use this to set up remotes, inspect history, or manage branches.

```bash
gopass git init
gopass git remote add origin git@github.com:you/passwords.git
gopass git log --oneline
gopass git status
```

All mutating commands (`insert`, `edit`, `rename`, `rm`) auto-commit to git if the store is a git repo.

### `sync`

Pulls remote changes (with rebase) then pushes local commits. Use this to sync between machines.

```bash
gopass sync
```

---

## Git setup (first time)

To enable sync between two PCs:

```bash
# On PC 1 — initialise git and push
gopass git init
gopass git remote add origin git@github.com:you/passwords.git
gopass git push -u origin main

# On PC 2 — clone into the store path configured during init
git clone git@github.com:you/passwords.git ~/gpg

# Daily use on either machine
gopass sync
```

> [!NOTE]
> The store contains only `.gpg` files — plaintext never touches git. The remote repository is safe to host on GitHub or any other service provided your GPG private key stays off the server.

---

## Secret file format

Secrets are standard GPG-encrypted files (`.gpg`). The convention is:

```
Line 1: password
Line 2+: key: value metadata (username, url, notes, etc.)
```

Example:

```
correct-horse-battery-staple
username: martin@example.com
url: https://example.com
```

`copy` copies line 1. `show <name> <field>` extracts a named field. `show` alone prints everything.

---

## Editor configuration

Set the `EDITOR` environment variable to control which editor opens for `insert --random` and `edit`. Arguments are supported:

```bash
export EDITOR="vim"
export EDITOR="code --wait"
export EDITOR="nano"
```

---

## Clipboard tool detection (Linux)

On Linux, `gopass copy` detects the available clipboard tool in this order:

| Tool | Session |
|------|---------|
| `wl-copy` / `wl-paste` | Wayland |
| `xclip` | X11 |
| `xsel` | X11 (fallback) |

Install one if missing:

```bash
# Debian/Ubuntu
sudo apt install wl-clipboard   # Wayland
sudo apt install xclip          # X11
```

---

## Security notes

- All secrets are encrypted with your default GPG key (`--default-recipient-self`)
- Encryption always writes to a temporary file first; the original is only replaced on success — a failed or interrupted write never corrupts existing data
- Editor temp files are created in `/dev/shm` (RAM-backed) on Linux where available, so plaintext does not touch disk; they are zero-wiped before deletion regardless
- Clipboard clear scheduling passes the expected clipboard value via environment variable, not command-line arguments — command-line arguments are world-readable in `/proc/PID/cmdline` on Linux; environment variables are owner-only via `/proc/PID/environ`
- `~/.pygpgpassrc` is created with mode `600`
- Secret names are validated to prevent directory traversal outside the store root

---

## Limitations

- No GPG agent integration — your key must be unlocked before running commands (or your agent must be running)
- Clipboard clear does not survive if `python3` is not in `PATH` when the background cleaner spawns
- On Windows, same-user processes can read environment variables of other processes — this affects the clipboard cleaner's 90-second window
- Temp file zero-wipe is best-effort; SSDs with wear leveling and filesystem journaling may retain data in sectors Python cannot control
