# pygpgpass

A minimal, zero-dependency, pure Python alternative to `gopass` and `pass`.

Linux first: it needs only Python 3 and `gpg`, both already on most distributions, and talks to `gpg` directly — no pip, no virtualenv, no root. The same script also runs on locked-down Windows machines from Git Bash, using the `gpg` bundled with Git for Windows (see [Windows (Git Bash)](#windows-git-bash)).

---

## Features

- **Full command set:** `init`, `list`, `find`, `show`, `copy`, `agent`, `insert`, `edit`, `cp`, `rename`/`mv`, `rm`, `git`, `sync`, `gui`
- **ASCII tree view:** Recursively lists secrets, hiding empty directories and non-password files
- **Clipboard:** `wl-copy` (Wayland), `xclip` or `xsel` (X11), auto-detected; also `pbcopy` (macOS) and `clip.exe` (Windows)
- **Clipboard auto-clear:** Copied password is cleared after 90 seconds, only if the clipboard hasn't changed
- **Pronounceable password generator:** Cryptographically secure via Python's `secrets` module
- **Atomic writes:** Encryption uses a temp file + atomic rename — a failed write never corrupts an existing secret
- **No plaintext on disk:** Secrets are typed straight into the terminal (password input hidden) and piped to `gpg` — no editor, no temp files
- **Path safety:** Secret names are bounds-checked against the store root — no directory traversal

---

## Prerequisites

1. **Python 3.7+**
2. **GnuPG 2.1+** (`gpg`) and **git** — packaged on every major distribution
3. A GPG key pair — run `gpg --gen-key` if you don't have one
4. Optional: a clipboard tool for `copy` (see [Clipboard tool detection](#clipboard-tool-detection-linux)), and tkinter for `gui`

---

## Installation & Setup

### 1. Download the script

Save `pygpgpass.py` to your home directory:

```bash
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
gopass agent <name> <keyfile>
gopass insert <name> [--random]
gopass edit <name>
gopass cp <src> <dst>
gopass rename <old> <new>
gopass mv <old> <new>
gopass rm <name>
gopass git <subcommand>
gopass sync
gopass gui
gopass help
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

### `agent`

Decrypts a secret and uses its **first non-blank line** as the passphrase to load an SSH private key into the running `ssh-agent` (`ssh-add`). The passphrase is supplied through a one-shot `SSH_ASKPASS` helper via an environment variable — it never appears on a command line, on disk, or in the clipboard.

```bash
gopass agent ssh/github ~/.ssh/id_ed25519
# Identity added: /home/you/.ssh/id_ed25519 (you@host)
```

If no agent is reachable through `SSH_AUTH_SOCK`, `agent` starts one at `~/.ssh/pygpgpass-agent.sock` (or reuses it if it is already running there). A program can't change its parent shell's environment, so point your shell at that socket once, in `~/.bashrc`:

```bash
export SSH_AUTH_SOCK="$HOME/.ssh/pygpgpass-agent.sock"
```

`agent` prints this hint whenever it uses its own agent and your shell isn't pointed at it yet. If your desktop already runs an agent (GNOME Keyring, KDE, `gpg-agent`), that one is used and nothing is started.

Requires `ssh-add` and `ssh-agent` from OpenSSH 8.4+ (older versions need `DISPLAY` set). On Windows use Git Bash's: the helper is a `sh` script.

### `insert`

Stores a new secret. Enter the password (hidden, asked twice to confirm), then any extra lines such as `user: bob`, then type `SAVE` on its own line to encrypt and save.

```bash
gopass insert personal/email
```

Use `--random` to generate a cryptographically secure, pronounceable 15-character password instead of typing one. You are still asked for extra lines; the password is not printed (use `copy` or `show`):

```bash
gopass insert personal/email --random
```

### `edit`

Replaces an existing secret: same prompts as `insert`, and what you type becomes the whole secret, so re-enter any fields you want to keep. Leave the password blank to cancel without changes. Use `insert` to create a new secret.

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

### `gui`

Opens a small desktop window (tkinter, part of the Python standard library):

- Search box (Ctrl+F) filters the secret tree as you type
- Selecting a secret decrypts it with the password line masked — **Show** reveals it, **Copy password** copies it (cleared after 90 seconds, same as `copy`)
- **New…** asks for a name and opens an in-window editor pre-filled with a generated password; **Generate password** replaces the password line while editing
- **Edit** / **Delete** work like the CLI commands, including the git commit and push
- If your GPG key is locked, the passphrase is asked for in a dialog

```bash
gopass gui
```

tkinter may need installing: `sudo dnf install python3-tkinter` (Fedora) or `sudo apt install python3-tk` (Debian/Ubuntu).

---

## Windows (Git Bash)

Windows is supported as a fallback for machines where Linux tools aren't allowed. Requirements:

- **Git for Windows** — provides Git Bash plus the `gpg`, `ssh-add` and `ssh-agent` used by the script
- **Python 3** for Windows (the python.org installer includes tkinter)
- Run it from Git Bash, ideally inside **Windows Terminal** rather than the legacy `mintty` console

Windows-specific behaviour, all handled automatically:

- The GPG passphrase is asked for in the terminal rather than in a graphical pinentry window (see [Passphrase prompt](#passphrase-prompt))
- `~` and Git Bash paths such as `/c/Users/you/gpg` are converted to native Windows paths
- `GNUPGHOME`, if set, is converted back to the `/c/...` form that Git's `gpg` expects
- Piped input and output use UTF-8 and `\n` line endings, as on Linux, so `pw=$(gopass show x 1)` works
- `copy` uses `clip.exe`; `agent` needs Git Bash's `ssh-add`, not `C:\Windows\System32\OpenSSH\ssh-add.exe`

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

## Passphrase prompt

On Linux, decryption uses gpg's own pinentry (curses or GTK/Qt, as configured in `gpg-agent.conf`), like `pass` and `gopass`.

On Windows the passphrase is asked for in the terminal (`GPG passphrase:`) instead, because Git's bundled pinentry is a graphical window. The passphrase is handed to gpg via loopback pinentry and cached by `gpg-agent` as usual, so you are only asked again once the agent cache expires.

Override the platform default in `~/.pygpgpassrc`, e.g. to get the terminal prompt on Linux too (handy over SSH):

```ini
[pygpgpass]
console_pinentry = true    # or false to use gpg's own pinentry
```

Requires `allow-loopback-pinentry` in `gpg-agent.conf`, which is on by default since GnuPG 2.1.12.

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
- `insert` and `edit` read secrets from the terminal and pipe them to `gpg`, so plaintext is never written to a file (the GUI edits in memory too)
- Clipboard clear scheduling passes the expected clipboard value via environment variable, not command-line arguments — command-line arguments are world-readable in `/proc/PID/cmdline` on Linux; environment variables are owner-only via `/proc/PID/environ`
- `~/.pygpgpassrc` is created with mode `600`
- Secret names are validated to prevent directory traversal outside the store root

---

## Limitations

- On Windows, same-user processes can read environment variables of other processes — this affects the clipboard cleaner's 90-second window
