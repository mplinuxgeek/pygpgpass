#!/usr/bin/env python3
import os
import sys
import subprocess
import tempfile
import secrets
import shutil
import time
import configparser
import re
import getpass

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog, font as tkfont
except ImportError:  # only needed for 'gopass gui'
    tk = None

# Force the standard output streams to use UTF-8, fixing the Windows CP1252 crash
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

# --- Windows / Git Bash compatibility ----------------------------------------
# Linux is the primary platform. Everything here is a no-op elsewhere: it only papers over
# native Windows Python run from Git Bash, using the MSYS gpg bundled with Git for Windows.

_WINDOWS = sys.platform == "win32"

def _msys_to_windows(path):
    """Convert a Git Bash style path (/c/Users/..., /tmp) to a native Windows path."""
    if shutil.which("cygpath"):
        try:
            r = subprocess.run(["cygpath", "-w", path], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except OSError:
            pass
    m = re.match(r"^/([a-zA-Z])(/.*)?$", path)
    if m:
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return path

def _fix_gnupghome_for_msys_gpg():
    """Git Bash hands native programs GNUPGHOME as C:/..., but Git for Windows' gpg is an
    MSYS build that only treats /c/... as absolute (it would look in <cwd>/C:/...).
    Convert it back when the gpg on PATH is an MSYS build; native gpg (Gpg4win) needs C:/..."""
    home = os.environ.get("GNUPGHOME", "")
    if not _WINDOWS or not re.match(r"^[A-Za-z]:[\\/]", home):
        return
    gpg = shutil.which("gpg")
    if gpg and os.path.exists(os.path.join(os.path.dirname(gpg), "msys-2.0.dll")):
        os.environ["GNUPGHOME"] = "/" + home[0].lower() + home[2:].replace("\\", "/")

def _utf8_pipes():
    """On Windows, Python decodes/encodes pipes with the ANSI code page (e.g. cp1252), so
    'printf ... | pass insert' or 'pass show x | ...' would mangle non-ASCII text. Git Bash
    pipes carry UTF-8, with LF line endings (a CR would end up in e.g. pw=$(pass show x 1)).
    Console streams are left alone: Python uses the Unicode console API."""
    if not _WINDOWS:
        return
    if hasattr(sys.stdin, "reconfigure") and not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8", newline="\n")

_fix_gnupghome_for_msys_gpg()

# -----------------------------------------------------------------------------

def _expand_path(path):
    """Expand ~ and Git Bash style paths to native ones.
    ~ follows $HOME when set, as Git Bash does; Python on Windows would otherwise use
    %USERPROFILE%, which differs from Git Bash's home on many corporate machines."""
    home = os.environ.get("HOME")
    if home and (path == "~" or path.startswith(("~/", "~\\"))):
        path = home + path[1:]
    else:
        path = os.path.expanduser(path)
    if _WINDOWS and path.startswith("/"):
        path = _msys_to_windows(path)
    return os.path.normpath(path)

def _config_file():
    path = _expand_path("~/.pygpgpassrc")
    legacy = os.path.expanduser("~/.pygpgpassrc")  # where older versions looked on Windows
    if not os.path.exists(path) and os.path.exists(legacy):
        return legacy
    return path

CONFIG_FILE     = _config_file()
SYNC_INTERVAL_H = 12

def _read_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")
    return config

def load_gpg_store_path():
    path = _read_config().get("pygpgpass", "store", fallback="").strip()
    if path:
        return _expand_path(path)
    return None

def generate_pronounceable_password(length=15, digit_length=3):
    """Baked-in custom pronounceable password generator logic (NicePass blueprint)."""
    # minimum: 1 uppercase consonant + 2 special chars + digit_length digits
    if length < (3 + digit_length):
        raise ValueError("Provided length parameters do not allow for a valid password composition.")

    consonants = 'bcdfghjklmnpqrstvwxyz'
    vowels = 'aeiou'
    special_chars = '!@#$%&*?'
    digits = '1234567890'

    password_parts = [secrets.choice(consonants).upper()]
    remaining_length = length - 1 - 2 - digit_length

    for i in range(remaining_length):
        if i % 2 == 0:
            password_parts.append(secrets.choice(vowels))
        else:
            password_parts.append(secrets.choice(consonants))

    password_parts.append(secrets.choice(special_chars))
    for _ in range(digit_length):
        password_parts.append(secrets.choice(digits))
    password_parts.append(secrets.choice(special_chars))

    return ''.join(password_parts)

def _resolve_secret_path(gpg_store, search_term):
    """Absolute file path for a secret name, or None if it would escape the store."""
    search_clean = search_term.replace("\\", "/")
    if search_clean.endswith(".gpg"):
        search_clean = search_clean[:-4]
    target = os.path.normpath(os.path.join(gpg_store, f"{search_clean}.gpg"))
    if not target.startswith(os.path.normpath(gpg_store) + os.sep):
        return None
    return target

def get_target_path(gpg_store, search_term):
    """Calculates the absolute file path for a given secret name."""
    target = _resolve_secret_path(gpg_store, search_term)
    if target is None:
        print(f"Error: Invalid secret name '{search_term}'.", file=sys.stderr)
        sys.exit(1)
    return target

def list_secret_names(gpg_store):
    """Sorted secret names (store-relative, '/'-separated, no .gpg suffix)."""
    names = []
    for root, dirs, files in os.walk(gpg_store):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # skip .git
        for f in files:
            if f.endswith(".gpg"):
                rel = os.path.relpath(os.path.join(root, f), gpg_store).replace("\\", "/")
                names.append(rel[:-4])
    return sorted(names)

def _use_console_pinentry():
    """Prompt for the passphrase in the terminal instead of gpg's pinentry window.
    Defaults on for Windows, where the bundled pinentry is graphical."""
    value = _read_config().get("pygpgpass", "console_pinentry", fallback="").strip().lower()
    if value:
        return value in ("1", "true", "yes", "on")
    return _WINDOWS

class DecryptError(Exception):
    pass

def _gpg_error(stderr):
    """gpg's own reason for a failure (last 'gpg:' line, status lines skipped), or ''."""
    lines = [l.strip() for l in stderr.splitlines()
             if l.strip() and not l.startswith("[GNUPG:]")]
    reasons = [l for l in lines if l.startswith("gpg:")]
    return (reasons or lines or [""])[-1]

def _console_passphrase(retry):
    if retry:
        print("Bad passphrase, try again.", file=sys.stderr)
    try:
        return getpass.getpass("GPG passphrase: ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None

def _decrypt_loopback(file_path, ask_passphrase):
    """Decrypt via loopback pinentry: try the agent's cached passphrase first,
    then ask_passphrase(retry) (None gives up). The agent caches whatever passphrase works."""
    base = ["gpg", "--quiet", "--batch", "--pinentry-mode", "loopback", "--status-fd", "2"]
    # Bytes, not text: on Windows text mode would turn "\n" into "\r\n" and use the
    # ANSI code page, either of which corrupts the passphrase gpg receives.
    def run(*args, passphrase=None):
        r = subprocess.run(base + list(args), capture_output=True,
                           input=None if passphrase is None else (passphrase + "\n").encode("utf-8"),
                           stdin=subprocess.DEVNULL if passphrase is None else None)
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")

    returncode, stdout, stderr = run("--decrypt", file_path)
    if returncode == 0:
        return stdout
    if "[GNUPG:] NEED_PASSPHRASE" in stderr:  # otherwise a passphrase won't help
        for attempt in range(3):
            passphrase = ask_passphrase(attempt > 0)
            if passphrase is None:
                break
            returncode, stdout, stderr = run("--passphrase-fd", "0", "--decrypt", file_path,
                                             passphrase=passphrase)
            if returncode == 0:
                return stdout
    reason = _gpg_error(stderr)
    raise DecryptError(f"Failed to decrypt {file_path}." + (f" ({reason})" if reason else ""))

def decrypt_file(file_path):
    """Invoke gpg to decrypt the file silently and return the text."""
    if _use_console_pinentry():
        try:
            return _decrypt_loopback(file_path, _console_passphrase)
        except DecryptError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    try:
        result = subprocess.run(
            ["gpg", "--quiet", "--decrypt", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True
        )
        # Bytes, not text mode: on Windows text mode would decode with the ANSI code page.
        return result.stdout.decode("utf-8", "replace")
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to decrypt {file_path}. Is your GPG key unlocked?", file=sys.stderr)
        reason = _gpg_error(e.stderr.decode("utf-8", "replace"))
        if reason:
            print(f"  {reason}", file=sys.stderr)
        sys.exit(1)

CLIPBOARD_CLEAR_SECONDS = 90

def _clipboard_cmds():
    """Returns (write_cmd, read_cmd) for the current platform, or (None, None) if unsupported."""
    if _WINDOWS:
        return ["clip.exe"], ["powershell.exe", "-command", "Get-Clipboard"]
    if sys.platform == "darwin":
        return ["pbcopy"], ["pbpaste"]
    # Linux: prefer Wayland, fall back to xclip then xsel
    for write, read in [
        (["wl-copy"],                              ["wl-paste"]),
        (["xclip", "-selection", "clipboard"],     ["xclip", "-selection", "clipboard", "-o"]),
        (["xsel",  "--clipboard", "--input"],      ["xsel",  "--clipboard", "--output"]),
    ]:
        if shutil.which(write[0]):
            return write, read
    return None, None

def copy_to_clipboard(text, write_cmd):
    """Copies text to the system clipboard using the provided write command."""
    try:
        subprocess.run(write_cmd, input=text.strip(), text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Warning: Failed to copy to clipboard.", file=sys.stderr)

def schedule_clipboard_clear(expected_text, write_cmd, read_cmd):
    """Spawns a detached background process that clears clipboard after timeout if still unchanged."""
    import json
    if not write_cmd or not read_cmd:
        return
    # Pass sensitive data via environment variables — not cmdline args.
    # /proc/PID/cmdline is world-readable on Linux; /proc/PID/environ is owner-only.
    env = os.environ.copy()
    env["_PYGPGPASS_PWD"]   = expected_text.strip()
    env["_PYGPGPASS_READ"]  = json.dumps(read_cmd)
    env["_PYGPGPASS_WRITE"] = json.dumps(write_cmd)
    env["_PYGPGPASS_DELAY"] = str(CLIPBOARD_CLEAR_SECONDS)
    script = (
        "import time,subprocess,json,os;"
        "time.sleep(int(os.environ['_PYGPGPASS_DELAY']));"
        "expected=os.environ['_PYGPGPASS_PWD'];"
        "read_cmd=json.loads(os.environ['_PYGPGPASS_READ']);"
        "write_cmd=json.loads(os.environ['_PYGPGPASS_WRITE']);"
        "r=subprocess.run(read_cmd,capture_output=True,text=True,timeout=5);"
        "r.stdout.strip()==expected and subprocess.run(write_cmd,input='',text=True)"
    )
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    if _WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-c", script], **kwargs)
    except Exception:
        print("Warning: Could not schedule clipboard clear.", file=sys.stderr)

AGENT_SOCKET = "~/.ssh/pygpgpass-agent.sock"

def _agent_reachable(sock):
    """True if an ssh-agent answers on sock ('ssh-add -l' exits 2 when it can't connect)."""
    env = dict(os.environ, SSH_AUTH_SOCK=sock)
    r = subprocess.run(["ssh-add", "-l"], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode in (0, 1)

def _ensure_ssh_agent():
    """Make sure an ssh-agent is reachable for this process, setting SSH_AUTH_SOCK.
    Uses the shell's agent if it answers, otherwise the one at AGENT_SOCKET, starting it if needed.
    A child process can't change the calling shell's environment, so the agent lives at a fixed
    path that the shell can point SSH_AUTH_SOCK at. Returns (socket, started) for AGENT_SOCKET,
    or None when the shell's own agent was used. Raises OSError if ssh-agent can't be started."""
    current = os.environ.get("SSH_AUTH_SOCK")
    if current and _agent_reachable(current):
        return None
    sock = _expand_path(AGENT_SOCKET)
    started = False
    if not _agent_reachable(sock):
        if os.path.exists(sock):
            os.remove(sock)  # stale socket left by an agent that has exited
        os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
        # stderr to a file, not a pipe: the daemonised agent could hold a pipe open and hang us
        with tempfile.TemporaryFile() as err:
            r = subprocess.run(["ssh-agent", "-a", sock], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=err)
            if r.returncode != 0:
                err.seek(0)
                raise OSError(err.read().decode("utf-8", "replace").strip() or "ssh-agent failed")
        started = True
    os.environ["SSH_AUTH_SOCK"] = sock
    return sock, started

def ssh_add_with_passphrase(key_file, passphrase):
    """Load key_file into ssh-agent, answering ssh-add's passphrase prompt via SSH_ASKPASS.
    The passphrase travels in an environment variable, never on a command line or on disk."""
    fd, askpass = tempfile.mkstemp(prefix="pygpgpass-askpass-", suffix=".sh")
    try:
        with os.fdopen(fd, "w", newline="\n") as f:
            # Answer once only: ssh-add re-asks on a wrong passphrase and would loop forever
            f.write('#!/bin/sh\n'
                    '[ -e "$_PYGPGPASS_ONCE" ] && exit 1\n'
                    ': > "$_PYGPGPASS_ONCE"\n'
                    'printf \'%s\\n\' "$_PYGPGPASS_PWD"\n')
        os.chmod(askpass, 0o700)
        env = os.environ.copy()
        env["_PYGPGPASS_PWD"]        = passphrase
        env["_PYGPGPASS_ONCE"]       = askpass + ".used"
        env["SSH_ASKPASS"]           = askpass
        env["SSH_ASKPASS_REQUIRE"]   = "force"  # OpenSSH 8.4+
        env.setdefault("DISPLAY", ":0")         # older OpenSSH only uses askpass with DISPLAY set and no tty on stdin
        return subprocess.run(["ssh-add", key_file], env=env, stdin=subprocess.DEVNULL).returncode
    except FileNotFoundError:
        print("Error: ssh-add not found in PATH.", file=sys.stderr)
        return 1
    finally:
        for path in (askpass, askpass + ".used"):
            if os.path.exists(path):
                os.remove(path)

def _encrypt(text_content, target_path):
    """Encrypt to target_path via temp file + atomic rename. Raises CalledProcessError on failure."""
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp_path = target_path + ".tmp"
    try:
        subprocess.run(
            ["gpg", "--quiet", "--yes", "--encrypt", "--default-recipient-self", "--output", tmp_path],
            # Bytes, not text mode: on Windows text mode would store CRLF line endings
            # and ANSI code page text instead of UTF-8.
            input=text_content.encode("utf-8"),
            check=True
        )
        os.replace(tmp_path, target_path)
    except subprocess.CalledProcessError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

def encrypt_text(text_content, target_path):
    """Encrypts plain text directly to a target .gpg file path."""
    try:
        _encrypt(text_content, target_path)
        print(f"Saved secret to {target_path}")
    except subprocess.CalledProcessError:
        print("Error: Encryption failed. Do you have a valid GPG key set up?", file=sys.stderr)
        sys.exit(1)

def _read_password():
    """Hidden password entry with confirmation. Returns "" if left blank."""
    if not sys.stdin.isatty():  # piped input: no echo to hide
        return sys.stdin.readline().rstrip("\r\n")
    while True:
        password = getpass.getpass("Password: ")
        if not password or getpass.getpass("Confirm password: ") == password:
            return password
        print("Passwords don't match, try again.", file=sys.stderr)

def _read_extra_lines():
    """Read optional lines after the password (e.g. 'user: bob') until SAVE or end of input."""
    if sys.stdin.isatty():
        print("Extra lines, e.g. 'user: bob' (type SAVE on its own line to finish):")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "SAVE":
            break
        lines.append(line)
    return "".join(line + "\n" for line in lines)

def _is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

def _has_remote(gpg_store):
    r = subprocess.run(["git", "remote"], cwd=gpg_store,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return bool(r.stdout.strip())

def _update_sync_stamp():
    try:
        config = _read_config()
        if "pygpgpass" not in config:
            return
        config["pygpgpass"]["last_sync"] = str(time.time())
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            config.write(f)
    except OSError:
        pass

def _git(gpg_store, *args):
    """Run a git command in the store, capturing output (C locale for stable messages)."""
    env = dict(os.environ, LC_ALL="C")
    return subprocess.run(["git", *args], cwd=gpg_store,
                          capture_output=True, text=True, env=env)

def _abort_stale_rebase(gpg_store):
    """Abort a rebase left in progress (e.g. by an earlier failed pull). Returns True if one was aborted."""
    git_dir = os.path.join(gpg_store, ".git")
    if any(os.path.isdir(os.path.join(git_dir, d)) for d in ("rebase-merge", "rebase-apply")):
        _git(gpg_store, "rebase", "--abort")
        return True
    return False

def _git_error_line(result):
    """Pick the most informative line from a failed git command's output."""
    lines = [l.strip() for l in (result.stderr + result.stdout).splitlines() if l.strip()]
    for line in lines:
        if line.startswith(("fatal:", "error:", "CONFLICT")):
            return line
    return lines[-1] if lines else f"git exited with {result.returncode}"

def _git_pull(gpg_store, tag):
    """Pull with rebase, auto-fixing common failure states. Returns (ok, message)."""
    if _abort_stale_rebase(gpg_store):
        print(f"{tag} Aborted an interrupted rebase.", file=sys.stderr)

    # --autostash: uncommitted changes in the store no longer block the pull
    result = _git(gpg_store, "pull", "--rebase", "--autostash")

    if result.returncode != 0 and "no tracking information" in result.stderr:
        branch = _git(gpg_store, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        remote = _git(gpg_store, "remote").stdout.split()[0]
        _git(gpg_store, "fetch", remote)
        if _git(gpg_store, "branch", f"--set-upstream-to={remote}/{branch}").returncode == 0:
            print(f"{tag} Set upstream to {remote}/{branch}.", file=sys.stderr)
            result = _git(gpg_store, "pull", "--rebase", "--autostash")

    if result.returncode == 0:
        return True, result.stdout.strip()

    if _abort_stale_rebase(gpg_store):
        # Encrypted files can't be merged; undo the half-done rebase so the store stays usable
        return False, ("local and remote changes conflict; local state restored. "
                       "Resolve with 'gopass git pull --rebase'.")
    return False, _git_error_line(result)

def _maybe_sync(gpg_store):
    """Pull from remote if the store hasn't been synced in SYNC_INTERVAL_H hours."""
    if not _is_git_repo(gpg_store):
        return
    try:
        if not _has_remote(gpg_store):
            return
    except FileNotFoundError:
        return

    now = time.time()
    try:
        last = float(_read_config().get("pygpgpass", "last_sync", fallback="0"))
        if now - last < SYNC_INTERVAL_H * 3600:
            return
    except ValueError:
        pass  # corrupt stamp — sync now

    print("[Auto-sync] Checking for remote changes...", file=sys.stderr)
    try:
        ok, out = _git_pull(gpg_store, "[Auto-sync]")
        if ok:
            _update_sync_stamp()
            if out and "Already up to date" not in out:
                print(out, file=sys.stderr)
        else:
            print(f"[Auto-sync] Pull failed: {out}", file=sys.stderr)
    except FileNotFoundError:
        pass

def _git_commit(gpg_store, message):
    """Stage all changes, commit, and push if a remote is configured."""
    if not _is_git_repo(gpg_store):
        return
    try:
        subprocess.run(["git", "add", "-A"], cwd=gpg_store,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        result = subprocess.run(["git", "commit", "-m", message], cwd=gpg_store)
        if result.returncode not in (0, 1):
            print("Warning: git commit failed.", file=sys.stderr)
            return
        if result.returncode == 1:
            return  # nothing to commit
        if not _has_remote(gpg_store):
            return
        push = subprocess.run(["git", "push"], cwd=gpg_store,
                              capture_output=True, text=True)
        if push.returncode == 0:
            print("[pushed to remote]", file=sys.stderr)
        else:
            print("Warning: git push failed — run 'gopass sync' to retry.", file=sys.stderr)
    except FileNotFoundError:
        print("Warning: git not found — store not committed.", file=sys.stderr)

def has_gpg_files(dir_path):
    """Returns True if the directory contains any .gpg files recursively."""
    for root, _, files in os.walk(dir_path):
        if any(f.endswith(".gpg") for f in files):
            return True
    return False

def print_tree(dir_path, prefix=""):
    """Recursively prints a tree of directories and .gpg files, hiding empty paths."""
    try:
        items = sorted(os.listdir(dir_path))
    except PermissionError:
        return

    valid_items = []
    for item in items:
        full_path = os.path.join(dir_path, item)
        if os.path.isdir(full_path):
            if has_gpg_files(full_path):
                valid_items.append(item)
        elif item.endswith(".gpg"):
            valid_items.append(item)

    for i, item in enumerate(valid_items):
        is_last = (i == len(valid_items) - 1)
        connector = "└── " if is_last else "├── "
        full_path = os.path.join(dir_path, item)

        if os.path.isdir(full_path):
            print(f"{prefix}{connector}{item}")
            next_prefix = prefix + ("    " if is_last else "│   ")
            print_tree(full_path, next_prefix)
        else:
            display_name = item[:-4]  # strip .gpg suffix
            print(f"{prefix}{connector}{display_name}")

class PasswordGui:
    """Minimal tkinter front end: browse, view, copy, add, edit and delete secrets."""

    MASK = "••••••••••••"

    def __init__(self, root, gpg_store):
        self.root = root
        self.store = gpg_store
        self.current = None    # name of the secret on display
        self.content = ""      # its decrypted text
        self.new_name = None   # set while composing a secret that isn't saved yet
        self.revealed = False
        self.editing = False

        root.title(f"pygpgpass — {gpg_store}")
        root.geometry("820x500")
        root.minsize(600, 340)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        style = ttk.Style(root)
        if sys.platform not in ("win32", "darwin") and "clam" in style.theme_names():
            style.theme_use("clam")  # Tk's X11 default theme looks dated; Windows/macOS keep native themes
        heading_font = tkfont.nametofont("TkDefaultFont").copy()
        heading_font.configure(weight="bold", size=heading_font.cget("size") + 2)

        # Left: search + tree of secrets
        left = ttk.Frame(root, padding=8)
        left.grid(row=0, column=0, sticky="ns")
        left.rowconfigure(2, weight=1)
        ttk.Label(left, text="Search (Ctrl+F)").grid(row=0, column=0, sticky="w")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.refresh())
        search_entry = ttk.Entry(left, textvariable=self.search, width=32)
        search_entry.grid(row=1, column=0, sticky="ew")
        search_entry.focus_set()
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.grid(row=2, column=0, sticky="ns", pady=6)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        scroll.grid(row=2, column=1, sticky="ns", pady=6)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.on_select())
        ttk.Button(left, text="New…", command=self.new_secret).grid(row=3, column=0, sticky="ew")

        # Right: secret details
        right = ttk.Frame(root, padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.heading = ttk.Label(right, font=heading_font)
        self.heading.grid(row=0, column=0, sticky="w")
        self.text = tk.Text(right, wrap="word", font="TkFixedFont", undo=True, state="disabled")
        self.text.grid(row=1, column=0, sticky="nsew", pady=6)

        self.view_bar = ttk.Frame(right)
        self.view_bar.grid(row=2, column=0, sticky="w")
        self.reveal_button = ttk.Button(self.view_bar, text="Show", command=self.toggle_reveal)
        self.view_buttons = [
            ttk.Button(self.view_bar, text="Copy password", command=self.copy_password),
            self.reveal_button,
            ttk.Button(self.view_bar, text="Edit", command=self.start_edit),
            ttk.Button(self.view_bar, text="Delete", command=self.delete_secret),
        ]
        for button in self.view_buttons:
            button.pack(side="left", padx=(0, 6))

        self.edit_bar = ttk.Frame(right)
        self.edit_bar.grid(row=2, column=0, sticky="w")
        for text, command in (("Save", self.save), ("Cancel", self.cancel_edit),
                              ("Generate password", self.generate_password)):
            ttk.Button(self.edit_bar, text=text, command=command).pack(side="left", padx=(0, 6))
        self.edit_bar.grid_remove()

        self.status = ttk.Label(right, foreground="gray")
        self.status.grid(row=3, column=0, sticky="w", pady=(6, 0))

        root.bind("<Control-f>", lambda _: search_entry.focus_set())
        self.refresh()
        self.show_secret(None)

    # --- helpers ---

    def set_status(self, message):
        self.status.configure(text=message)

    def set_text(self, text, editable=False):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_reset()
        if not editable:
            self.text.configure(state="disabled")

    def masked_content(self):
        """The secret with its password line (first non-blank) hidden."""
        lines = self.content.split("\n")
        for i, line in enumerate(lines):
            if line.strip():
                lines[i] = self.MASK
                break
        return "\n".join(lines)

    def ask_passphrase(self, retry):
        prompt = "Bad passphrase, try again:" if retry else "Passphrase for your GPG key:"
        return simpledialog.askstring("Unlock GPG key", prompt, show="*", parent=self.root)

    def confirm_discard(self):
        return not self.editing or messagebox.askyesno(
            "Discard changes?", "You have unsaved changes. Discard them?", parent=self.root)

    # --- tree ---

    def refresh(self, select=None):
        """Rebuild the tree: folders when unfiltered, a flat match list while searching."""
        self.tree.delete(*self.tree.get_children())
        term = self.search.get().strip().lower()
        for name in list_secret_names(self.store):
            if term:
                if term in name.lower():
                    self.tree.insert("", "end", iid=name, text=name)
                continue
            parts = name.split("/")
            parent = ""
            for depth in range(len(parts) - 1):
                folder = "/".join(parts[:depth + 1]) + "/"  # trailing slash keeps folder ids apart from secrets
                if not self.tree.exists(folder):
                    self.tree.insert(parent, "end", iid=folder, text=parts[depth] + "/")
                parent = folder
            self.tree.insert(parent, "end", iid=name, text=parts[-1])
        if select and self.tree.exists(select):
            self.tree.see(select)
            self.tree.selection_set(select)

    def on_select(self):
        selection = self.tree.selection()
        if not selection or selection[0].endswith("/") or selection[0] == self.current:
            return
        if not self.confirm_discard():
            return
        self.end_edit()
        name = selection[0]
        try:
            content = _decrypt_loopback(get_target_path(self.store, name), self.ask_passphrase)
        except DecryptError as e:
            self.show_secret(None)
            self.set_status(str(e))
            return
        self.show_secret(name, content)

    # --- view mode ---

    def show_secret(self, name, content=""):
        self.current, self.content, self.revealed = name, content, False
        self.heading.configure(text=name or "No secret selected")
        self.reveal_button.configure(text="Show")
        self.set_text(self.masked_content())
        for button in self.view_buttons:
            button.state(["!disabled"] if name else ["disabled"])
        self.set_status("")

    def toggle_reveal(self):
        self.revealed = not self.revealed
        self.reveal_button.configure(text="Hide" if self.revealed else "Show")
        self.set_text(self.content if self.revealed else self.masked_content())

    def copy_password(self):
        password = _first_line(self.content)
        write_cmd, read_cmd = _clipboard_cmds()
        if write_cmd:
            copy_to_clipboard(password, write_cmd)
            schedule_clipboard_clear(password, write_cmd, read_cmd)
        else:
            # No system tool: use Tk's clipboard (lost when the window closes on X11)
            self.root.clipboard_clear()
            self.root.clipboard_append(password)
            self.root.after(CLIPBOARD_CLEAR_SECONDS * 1000, self.clear_tk_clipboard, password)
        self.set_status(f"Copied password for '{self.current}' — clears in {CLIPBOARD_CLEAR_SECONDS}s")

    def clear_tk_clipboard(self, expected):
        try:
            if self.root.clipboard_get() == expected:
                self.root.clipboard_clear()
        except tk.TclError:
            pass

    def delete_secret(self):
        name = self.current
        if not messagebox.askyesno("Delete secret", f"Delete '{name}'?", parent=self.root):
            return
        target = get_target_path(self.store, name)
        os.remove(target)
        _prune_empty_dirs(os.path.dirname(target), self.store)
        _git_commit(self.store, f"Remove {name}")
        self.show_secret(None)
        self.refresh()
        self.set_status(f"Removed '{name}'")

    # --- edit mode ---

    def start_edit(self, content=None):
        self.editing = True
        self.set_text(self.content if content is None else content, editable=True)
        self.view_bar.grid_remove()
        self.edit_bar.grid()
        self.text.focus_set()

    def end_edit(self):
        self.editing = False
        self.new_name = None
        self.edit_bar.grid_remove()
        self.view_bar.grid()

    def new_secret(self):
        if not self.confirm_discard():
            return
        name = simpledialog.askstring("New secret", "Name (e.g. web/github):", parent=self.root)
        name = (name or "").strip().strip("/")
        if not name:
            return
        target = _resolve_secret_path(self.store, name)
        if target is None:
            messagebox.showerror("New secret", f"Invalid secret name '{name}'.", parent=self.root)
            return
        if os.path.exists(target):
            messagebox.showerror("New secret", f"'{name}' already exists.", parent=self.root)
            return
        self.end_edit()
        self.show_secret(None)
        self.tree.selection_set(())
        self.new_name = name
        self.heading.configure(text=f"{name} (new)")
        self.start_edit(generate_pronounceable_password(length=15, digit_length=3) + "\n")
        self.set_status("Line 1 is the password. Add 'field: value' lines below if needed.")

    def generate_password(self):
        """Replace the password line (first non-blank) with a freshly generated one."""
        lines = self.text.get("1.0", "end-1c").split("\n")
        password = generate_pronounceable_password(length=15, digit_length=3)
        for i, line in enumerate(lines):
            if line.strip():
                lines[i] = password
                break
        else:
            lines.insert(0, password)
        self.set_text("\n".join(lines), editable=True)

    def save(self):
        content = self.text.get("1.0", "end-1c")
        if not content.strip():
            self.set_status("Nothing to save.")
            return
        if not content.endswith("\n"):
            content += "\n"
        name = self.new_name or self.current
        try:
            _encrypt(content, get_target_path(self.store, name))
        except subprocess.CalledProcessError:
            messagebox.showerror("Save failed", "Encryption failed. Do you have a valid GPG key set up?",
                                 parent=self.root)
            return
        _git_commit(self.store, f"{'Insert' if self.new_name else 'Edit'} {name}")
        self.end_edit()
        self.show_secret(name, content)
        self.refresh(select=name)
        self.set_status(f"Saved '{name}'")

    def cancel_edit(self):
        new = self.new_name
        self.end_edit()
        if new:
            self.show_secret(None)
        else:
            self.show_secret(self.current, self.content)

def run_gui(gpg_store):
    if tk is None:
        print("Error: tkinter is not available (on Linux install python3-tkinter or python3-tk).", file=sys.stderr)
        sys.exit(1)
    root = tk.Tk()
    PasswordGui(root, gpg_store)
    root.mainloop()

def _require_args(count, usage_line):
    """Exit with a usage error unless at least `count` arguments follow the command."""
    if len(sys.argv) < count + 2:
        print(f"Error: Missing arguments. (Usage: {usage_line})", file=sys.stderr)
        sys.exit(1)

def _existing_secret(gpg_store, name):
    """Return the path of an existing secret, or exit with an error."""
    target = get_target_path(gpg_store, name)
    if not os.path.isfile(target):
        print(f"Error: Password '{name}' does not exist.", file=sys.stderr)
        sys.exit(1)
    return target

def _new_secret(gpg_store, name):
    """Return the path for a secret that must not exist yet, or exit with an error."""
    target = get_target_path(gpg_store, name)
    if os.path.exists(target):
        print(f"Error: Password '{name}' already exists.", file=sys.stderr)
        sys.exit(1)
    return target

def _first_line(content):
    """The password: first non-blank line of a secret."""
    return next((l for l in content.splitlines() if l.strip()), "").strip()

def _prune_empty_dirs(path, gpg_store):
    """Remove now-empty directories from path up to (not including) the store root."""
    while path != gpg_store and os.path.isdir(path) and not os.listdir(path):
        os.rmdir(path)
        path = os.path.dirname(path)

def main():
    _utf8_pipes()
    usage = (
        "Usage:\n"
        "  gopass init [--force]\n"
        "  gopass list\n"
        "  gopass find <term>\n"
        "  gopass show <name> [<line>|<field>]\n"
        "  gopass copy <name>\n"
        "  gopass agent <name> <keyfile>\n"
        "  gopass insert <name> [--random]\n"
        "  gopass edit <name>\n"
        "  gopass cp <src> <dst>\n"
        "  gopass rename <old> <new>\n"
        "  gopass mv <old> <new>\n"
        "  gopass rm <name>\n"
        "  gopass git <subcommand>\n"
        "  gopass sync\n"
        "  gopass gui\n"
        "  gopass help"
    )

    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    if command in ("help", "-h", "--help"):
        print(usage)
        sys.exit(0)

    # --- COMMAND: INIT ---
    if command == "init":
        force = "--force" in sys.argv[2:]
        if os.path.exists(CONFIG_FILE) and not force:
            existing = load_gpg_store_path()
            print(f"Already initialized. Store path: {existing}", file=sys.stderr)
            print(f"Use 'gopass init --force' to reconfigure.", file=sys.stderr)
            sys.exit(1)
        print("Initializing pygpgpass store...")
        default_path = "~/gpg"
        user_path = input(f"Enter storage path for your passwords [default: {default_path}]: ").strip()

        if not user_path:
            user_path = default_path
        if not user_path.startswith("~"):
            user_path = os.path.abspath(_expand_path(user_path))  # a relative path would resolve against each command's cwd

        resolved_path = _expand_path(user_path)
        os.makedirs(resolved_path, exist_ok=True)

        config = _read_config()  # --force keeps any other settings
        if "pygpgpass" not in config:
            config["pygpgpass"] = {}
        config["pygpgpass"]["store"] = user_path
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            config.write(f)
        os.chmod(CONFIG_FILE, 0o600)

        print(f"Success! Password store initialized at: {resolved_path}")
        print(f"Configuration saved to: {CONFIG_FILE}")
        sys.exit(0)

    # Enforce initialization for all other commands
    gpg_store = load_gpg_store_path()
    if not gpg_store:
        print("Error: Password store not initialized yet. Please run 'gopass init' first.", file=sys.stderr)
        sys.exit(1)

    if command not in ("git", "sync"):
        _maybe_sync(gpg_store)

    # --- COMMAND: GUI ---
    if command == "gui":
        run_gui(gpg_store)

    # --- COMMAND: LIST ---
    elif command == "list":
        print(f"pygpgpass ({gpg_store})")
        print_tree(gpg_store)

    # --- COMMAND: FIND ---
    elif command == "find":
        _require_args(1, "gopass find <term>")
        term = sys.argv[2].lower()
        matches = [name for name in list_secret_names(gpg_store) if term in name.lower()]
        if matches:
            for m in matches:
                print(m)
        else:
            print(f"No secrets found matching '{term}'.", file=sys.stderr)
            sys.exit(1)

    # --- COMMAND: SHOW ---
    elif command == "show":
        _require_args(1, "gopass show <name> [<line>|<field>]")
        secret_content = decrypt_file(_existing_secret(gpg_store, sys.argv[2]))

        if len(sys.argv) >= 4:
            field = sys.argv[3]
            lines = secret_content.splitlines()
            if field.isdigit():
                idx = int(field) - 1
                if 0 <= idx < len(lines):
                    print(lines[idx])
                else:
                    print(f"Error: Line {field} does not exist (secret has {len(lines)} lines).", file=sys.stderr)
                    sys.exit(1)
            else:
                key = field.lower()
                for line in lines[1:]:
                    if line.lower().startswith(key + ":"):
                        print(line[len(key) + 1:].strip())
                        break
                else:
                    print(f"Error: Field '{field}' not found.", file=sys.stderr)
                    sys.exit(1)
        else:
            print(secret_content, end="")

    # --- COMMAND: COPY ---
    elif command == "copy":
        _require_args(1, "gopass copy <name>")
        target = _existing_secret(gpg_store, sys.argv[2])
        write_cmd, read_cmd = _clipboard_cmds()
        if not write_cmd:
            print("Error: No clipboard tool found (install xclip, xsel, or wl-clipboard).", file=sys.stderr)
            sys.exit(1)
        password = _first_line(decrypt_file(target))
        copy_to_clipboard(password, write_cmd)
        schedule_clipboard_clear(password, write_cmd, read_cmd)
        print(f"[Copied '{sys.argv[2]}' to clipboard — clears in {CLIPBOARD_CLEAR_SECONDS}s]", file=sys.stderr)

    # --- COMMAND: AGENT ---
    elif command == "agent":
        _require_args(2, "gopass agent <name> <keyfile>")
        target = _existing_secret(gpg_store, sys.argv[2])
        key_file = _expand_path(sys.argv[3])
        if not os.path.isfile(key_file):
            print(f"Error: SSH key file '{sys.argv[3]}' does not exist.", file=sys.stderr)
            sys.exit(1)
        passphrase = _first_line(decrypt_file(target))
        shell_sock = os.environ.get("SSH_AUTH_SOCK", "")
        try:
            own = _ensure_ssh_agent()
        except FileNotFoundError:
            print("Error: ssh-add/ssh-agent not found in PATH.", file=sys.stderr)
            sys.exit(1)
        except OSError as e:
            print(f"Error: Could not start ssh-agent: {e}", file=sys.stderr)
            sys.exit(1)
        rc = ssh_add_with_passphrase(key_file, passphrase)
        if rc != 0:
            print(f"Error: ssh-add failed. Is the first line of '{sys.argv[2]}' the key's passphrase?", file=sys.stderr)
        elif own:
            sock, started = own
            print(f"[{'Started' if started else 'Using'} ssh-agent at {AGENT_SOCKET}]", file=sys.stderr)
            shell_uses_it = bool(shell_sock) and os.path.normcase(_expand_path(shell_sock)) == os.path.normcase(sock)
            if not shell_uses_it:
                print("  Your shell isn't using this agent yet. Add to ~/.bashrc, then open a new shell:\n"
                      '  export SSH_AUTH_SOCK="$HOME/.ssh/pygpgpass-agent.sock"', file=sys.stderr)
        sys.exit(rc)

    # --- COMMAND: INSERT ---
    elif command == "insert":
        _require_args(1, "gopass insert <name> [--random]")
        secret_name = sys.argv[2]
        target = get_target_path(gpg_store, secret_name)
        if os.path.exists(target):
            print(f"Error: A password already exists at '{secret_name}'. Use 'edit' instead.", file=sys.stderr)
            sys.exit(1)

        try:
            if "--random" in sys.argv[3:]:
                password = generate_pronounceable_password(length=15, digit_length=3)
                print(f"Generated a random password for '{secret_name}' (copy it with 'gopass copy {secret_name}').")
            else:
                print(f"Enter the password for '{secret_name}' (input is hidden).")
                password = _read_password()
                if not password:
                    print("Aborted: No password provided.", file=sys.stderr)
                    sys.exit(0)
            secret_content = password + "\n" + _read_extra_lines()
        except KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        encrypt_text(secret_content, target)
        _git_commit(gpg_store, f"Insert {secret_name}")

    # --- COMMAND: EDIT ---
    elif command == "edit":
        _require_args(1, "gopass edit <name>")
        secret_name = sys.argv[2]
        target = _existing_secret(gpg_store, secret_name)
        print(f"Enter the new contents for '{secret_name}'. This replaces the whole secret;")
        print("leave the password blank to cancel. (Password input is hidden.)")
        try:
            password = _read_password()
            if not password:
                print("Aborted: Secret unchanged.", file=sys.stderr)
                sys.exit(0)
            secret_content = password + "\n" + _read_extra_lines()
        except KeyboardInterrupt:
            print("\nAborted: Secret unchanged.", file=sys.stderr)
            sys.exit(1)
        encrypt_text(secret_content, target)
        _git_commit(gpg_store, f"Edit {secret_name}")

    # --- COMMAND: CP ---
    elif command == "cp":
        _require_args(2, "gopass cp <src> <dst>")
        src_name, dst_name = sys.argv[2], sys.argv[3]
        src_target = _existing_secret(gpg_store, src_name)
        dst_target = _new_secret(gpg_store, dst_name)
        os.makedirs(os.path.dirname(dst_target), exist_ok=True)
        shutil.copy2(src_target, dst_target)
        print(f"Copied '{src_name}' to '{dst_name}'.")
        _git_commit(gpg_store, f"Copy {src_name} to {dst_name}")

    # --- COMMAND: RENAME / MV ---
    elif command in ("rename", "mv"):
        _require_args(2, f"gopass {command} <old> <new>")
        old_name, new_name = sys.argv[2], sys.argv[3]
        old_target = _existing_secret(gpg_store, old_name)
        new_target = _new_secret(gpg_store, new_name)
        os.makedirs(os.path.dirname(new_target), exist_ok=True)
        os.rename(old_target, new_target)
        _prune_empty_dirs(os.path.dirname(old_target), gpg_store)
        print(f"Renamed '{old_name}' to '{new_name}'.")
        _git_commit(gpg_store, f"Rename {old_name} to {new_name}")

    # --- COMMAND: RM ---
    elif command == "rm":
        _require_args(1, "gopass rm <name>")
        secret_name = sys.argv[2]
        target = _existing_secret(gpg_store, secret_name)

        # Enforce safety prompt confirmation
        try:
            confirm = input(f"Are you sure you want to delete '{secret_name}'? [y/N]: ").strip().lower()
            if confirm in ['y', 'yes']:
                os.remove(target)
                _prune_empty_dirs(os.path.dirname(target), gpg_store)
                print(f"Removed secret: {secret_name}")
                _git_commit(gpg_store, f"Remove {secret_name}")
            else:
                print("Aborted. Secret was not deleted.")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

    # --- COMMAND: GIT ---
    elif command == "git":
        _require_args(1, "gopass git <subcommand>")
        git_args = sys.argv[2:]
        if git_args[0] != "init" and not _is_git_repo(gpg_store):
            print("Error: Store is not a git repo. Run 'gopass git init' first.", file=sys.stderr)
            sys.exit(1)
        try:
            result = subprocess.run(["git"] + git_args, cwd=gpg_store)
            sys.exit(result.returncode)
        except FileNotFoundError:
            print("Error: git not found in PATH.", file=sys.stderr)
            sys.exit(1)

    # --- COMMAND: SYNC ---
    elif command == "sync":
        if not _is_git_repo(gpg_store):
            print("Error: Store is not a git repo. Run 'gopass git init' first.", file=sys.stderr)
            sys.exit(1)
        try:
            print("Pulling...")
            ok, out = _git_pull(gpg_store, "[sync]")
            if out:
                print(out, file=None if ok else sys.stderr)
            if not ok:
                print("Error: Pull failed.", file=sys.stderr)
                sys.exit(1)
            print("Pushing...")
            push = subprocess.run(["git", "push"], cwd=gpg_store)
            if push.returncode == 0:
                _update_sync_stamp()
            sys.exit(push.returncode)
        except FileNotFoundError:
            print("Error: git not found in PATH.", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Unknown command '{command}'.\n\n{usage}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
