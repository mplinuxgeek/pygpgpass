#!/usr/bin/env python3
import os
import sys
import subprocess
import tempfile
import secrets
import shutil
import shlex
import time
import configparser

# Force the standard output streams to use UTF-8, fixing the Windows CP1252 crash
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')

CONFIG_FILE     = os.path.expanduser("~/.pygpgpassrc")
SYNC_INTERVAL_H = 12

def _read_config():
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")
    return config

def load_gpg_store_path():
    path = _read_config().get("pygpgpass", "store", fallback="").strip()
    if path:
        return os.path.normpath(os.path.expanduser(path))
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

def get_target_path(gpg_store, search_term):
    """Calculates the absolute file path for a given secret name."""
    search_clean = search_term.replace("\\", "/")
    if search_clean.endswith(".gpg"):
        search_clean = search_clean[:-4]
    target = os.path.normpath(os.path.join(gpg_store, f"{search_clean}.gpg"))
    if not target.startswith(os.path.normpath(gpg_store) + os.sep):
        print(f"Error: Invalid secret name '{search_term}'.", file=sys.stderr)
        sys.exit(1)
    return target

def decrypt_file(file_path):
    """Invoke gpg to decrypt the file silently and return the text."""
    try:
        result = subprocess.run(
            ["gpg", "--quiet", "--decrypt", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError:
        print(f"Error: Failed to decrypt {file_path}. Is your GPG key unlocked?", file=sys.stderr)
        sys.exit(1)

CLIPBOARD_CLEAR_SECONDS = 90

def _clipboard_cmds():
    """Returns (write_cmd, read_cmd) for the current platform, or (None, None) if unsupported."""
    if sys.platform == "win32":
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
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(["python3", "-c", script], **kwargs)
    except Exception:
        print("Warning: Could not schedule clipboard clear.", file=sys.stderr)

def encrypt_text(text_content, target_path):
    """Encrypts plain text directly to a target .gpg file path."""
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    tmp_path = target_path + ".tmp"
    try:
        subprocess.run(
            ["gpg", "--quiet", "--yes", "--encrypt", "--default-recipient-self", "--output", tmp_path],
            input=text_content,
            text=True,
            check=True
        )
        os.replace(tmp_path, target_path)
        print(f"Saved secret to {target_path}")
    except subprocess.CalledProcessError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        print("Error: Encryption failed. Do you have a valid GPG key set up?", file=sys.stderr)
        sys.exit(1)

def launch_editor(initial_content, target_path, save_unchanged=False):
    """Opens a terminal editor with initial content and encrypts modifications."""
    editor_str = os.environ.get("EDITOR", "nano")
    editor_cmd = shlex.split(editor_str)
    # Prefer /dev/shm (RAM-backed tmpfs on Linux) so plaintext never hits disk.
    tmpdir = "/dev/shm" if os.path.isdir("/dev/shm") else None
    with tempfile.NamedTemporaryFile(suffix=".tmp", mode="w+", encoding="utf-8",
                                     delete=False, dir=tmpdir) as temp_file:
        temp_file.write(initial_content)
        temp_file_name = temp_file.name

    try:
        result = subprocess.run(editor_cmd + [temp_file_name])
        if result.returncode != 0:
            print(f"Editor exited with code {result.returncode}. Aborted.", file=sys.stderr)
            return
        with open(temp_file_name, "r", encoding="utf-8") as updated_file:
            new_content = updated_file.read()

        if not new_content.strip():
            print("Aborted: No content provided.")
        elif new_content == initial_content and not save_unchanged:
            print("Aborted: No modifications detected.")
        else:
            encrypt_text(new_content, target_path)
    finally:
        if os.path.exists(temp_file_name):
            try:
                with open(temp_file_name, "r+b") as f:
                    size = os.path.getsize(temp_file_name)
                    f.write(b"\x00" * size)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass
            os.remove(temp_file_name)

def _is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))

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

def _maybe_sync(gpg_store):
    """Pull from remote if the store hasn't been synced in SYNC_INTERVAL_H hours."""
    if not _is_git_repo(gpg_store):
        return
    try:
        r = subprocess.run(["git", "remote"], cwd=gpg_store,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if not r.stdout.strip():
            return  # no remote configured
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
        result = subprocess.run(["git", "pull", "--rebase"], cwd=gpg_store,
                                capture_output=True, text=True)
        if result.returncode == 0:
            _update_sync_stamp()
            out = result.stdout.strip()
            if out and "Already up to date" not in out:
                print(out, file=sys.stderr)
        else:
            print("[Auto-sync] Pull failed — run 'gopass sync' to resolve.", file=sys.stderr)
    except FileNotFoundError:
        pass

def _git_commit(gpg_store, message):
    """Stage all changes and commit. Silent no-op if store is not a git repo or git is absent."""
    if not _is_git_repo(gpg_store):
        return
    try:
        subprocess.run(["git", "add", "-A"], cwd=gpg_store,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        result = subprocess.run(["git", "commit", "-m", message], cwd=gpg_store)
        if result.returncode not in (0, 1):
            print("Warning: git commit failed.", file=sys.stderr)
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

def main():
    usage = (
        "Usage:\n"
        "  gopass init [--force]\n"
        "  gopass list\n"
        "  gopass find <term>\n"
        "  gopass show <name> [<line>|<field>]\n"
        "  gopass copy <name>\n"
        "  gopass insert <name> [--random]\n"
        "  gopass edit <name>\n"
        "  gopass cp <src> <dst>\n"
        "  gopass rename <old> <new>\n"
        "  gopass mv <old> <new>\n"
        "  gopass rm <name>\n"
        "  gopass git <subcommand>\n"
        "  gopass sync"
    )

    if len(sys.argv) < 2:
        print(usage, file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

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

        resolved_path = os.path.expanduser(user_path)
        os.makedirs(resolved_path, exist_ok=True)

        config = configparser.ConfigParser()
        config["pygpgpass"] = {"store": user_path}
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

    # --- COMMAND: LIST ---
    if command == "list":
        print(f"pygpgpass ({gpg_store})")
        print_tree(gpg_store)

    # --- COMMAND: FIND ---
    elif command == "find":
        if len(sys.argv) < 3:
            print("Error: Specify a search term. (Usage: gopass find <term>)", file=sys.stderr)
            sys.exit(1)
        term = sys.argv[2].lower()
        matches = []
        for root, _, files in os.walk(gpg_store):
            for f in files:
                if f.endswith(".gpg"):
                    rel = os.path.relpath(os.path.join(root, f), gpg_store).replace("\\", "/")
                    name = rel[:-4]
                    if term in name.lower():
                        matches.append(name)
        if matches:
            for m in sorted(matches):
                print(m)
        else:
            print(f"No secrets found matching '{term}'.", file=sys.stderr)
            sys.exit(1)

    # --- COMMAND: SHOW ---
    elif command == "show":
        if len(sys.argv) < 3:
            print("Error: Specify a password to show. (Usage: gopass show <name> [<line>|<field>])", file=sys.stderr)
            sys.exit(1)

        target = get_target_path(gpg_store, sys.argv[2])
        if not os.path.isfile(target):
            print(f"Error: Password '{sys.argv[2]}' does not exist.", file=sys.stderr)
            sys.exit(1)

        secret_content = decrypt_file(target)

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
        if len(sys.argv) < 3:
            print("Error: Specify a password to copy. (Usage: gopass copy <name>)", file=sys.stderr)
            sys.exit(1)

        target = get_target_path(gpg_store, sys.argv[2])
        if os.path.isfile(target):
            write_cmd, read_cmd = _clipboard_cmds()
            if not write_cmd:
                print("Warning: No clipboard tool found (install xclip, xsel, or wl-clipboard).", file=sys.stderr)
                sys.exit(1)
            secret_content = decrypt_file(target)
            first_line = next((l for l in secret_content.splitlines() if l.strip()), "")
            copy_to_clipboard(first_line, write_cmd)
            schedule_clipboard_clear(first_line, write_cmd, read_cmd)
            print(f"[Copied '{sys.argv[2]}' to clipboard — clears in {CLIPBOARD_CLEAR_SECONDS}s]", file=sys.stderr)
        else:
            print(f"Error: Password '{sys.argv[2]}' does not exist.", file=sys.stderr)
            sys.exit(1)

    # --- COMMAND: INSERT ---
    elif command == "insert":
        if len(sys.argv) < 3:
            print("Error: Specify a name for the new password. (Usage: gopass insert <name> [--random])", file=sys.stderr)
            sys.exit(1)

        secret_name = sys.argv[2]
        target = get_target_path(gpg_store, secret_name)

        if os.path.exists(target):
            print(f"Error: A password already exists at '{secret_name}'. Use 'edit' instead.", file=sys.stderr)
            sys.exit(1)

        if len(sys.argv) >= 4 and sys.argv[3] == "--random":
            generated_password = generate_pronounceable_password(length=15, digit_length=3) + "\n"
            print(f"Generating random password for '{secret_name}' and launching editor to review...")
            launch_editor(generated_password, target, save_unchanged=True)
            if os.path.exists(target):
                _git_commit(gpg_store, f"Insert {secret_name}")
        else:
            print(f"Enter data for {secret_name} (Type 'SAVE' on a new line and hit Enter when finished):")
            try:
                lines = []
                while True:
                    line = input()
                    if line.strip() == "SAVE":
                        break
                    lines.append(line)

                user_data = "\n".join(lines) + "\n"
                if not user_data.strip():
                    print("Aborted: No data provided.", file=sys.stderr)
                    sys.exit(0)
                encrypt_text(user_data, target)
                _git_commit(gpg_store, f"Insert {secret_name}")
            except (KeyboardInterrupt, EOFError):
                print("\nAborted.", file=sys.stderr)
                sys.exit(1)

    # --- COMMAND: EDIT ---
    elif command == "edit":
        if len(sys.argv) < 3:
            print("Error: Specify a password to edit. (Usage: gopass edit <name>)", file=sys.stderr)
            sys.exit(1)

        secret_name = sys.argv[2]
        target = get_target_path(gpg_store, secret_name)
        current_content = ""
        if os.path.isfile(target):
            current_content = decrypt_file(target)

        mtime_before = os.path.getmtime(target) if os.path.exists(target) else None
        launch_editor(current_content, target)
        mtime_after = os.path.getmtime(target) if os.path.exists(target) else None
        if mtime_after is not None and mtime_after != mtime_before:
            _git_commit(gpg_store, f"Edit {secret_name}")

    # --- COMMAND: CP ---
    elif command == "cp":
        if len(sys.argv) < 4:
            print("Error: Specify source and destination. (Usage: gopass cp <src> <dst>)", file=sys.stderr)
            sys.exit(1)

        src_name = sys.argv[2]
        dst_name = sys.argv[3]
        src_target = get_target_path(gpg_store, src_name)
        dst_target = get_target_path(gpg_store, dst_name)

        if not os.path.isfile(src_target):
            print(f"Error: Password '{src_name}' does not exist.", file=sys.stderr)
            sys.exit(1)

        if os.path.exists(dst_target):
            print(f"Error: Password '{dst_name}' already exists.", file=sys.stderr)
            sys.exit(1)

        os.makedirs(os.path.dirname(dst_target), exist_ok=True)
        shutil.copy2(src_target, dst_target)
        print(f"Copied '{src_name}' to '{dst_name}'.")
        _git_commit(gpg_store, f"Copy {src_name} to {dst_name}")

    # --- COMMAND: RENAME / MV ---
    elif command in ("rename", "mv"):
        if len(sys.argv) < 4:
            print(f"Error: Specify old and new names. (Usage: gopass {command} <old> <new>)", file=sys.stderr)
            sys.exit(1)

        old_name = sys.argv[2]
        new_name = sys.argv[3]
        old_target = get_target_path(gpg_store, old_name)
        new_target = get_target_path(gpg_store, new_name)

        if not os.path.isfile(old_target):
            print(f"Error: Password '{old_name}' does not exist.", file=sys.stderr)
            sys.exit(1)

        if os.path.exists(new_target):
            print(f"Error: Password '{new_name}' already exists.", file=sys.stderr)
            sys.exit(1)

        os.makedirs(os.path.dirname(new_target), exist_ok=True)
        os.rename(old_target, new_target)
        print(f"Renamed '{old_name}' to '{new_name}'.")
        _git_commit(gpg_store, f"Rename {old_name} to {new_name}")

        parent_dir = os.path.dirname(old_target)
        while parent_dir != gpg_store:
            if not os.listdir(parent_dir):
                os.rmdir(parent_dir)
                parent_dir = os.path.dirname(parent_dir)
            else:
                break

    # --- COMMAND: RM ---
    elif command == "rm":
        if len(sys.argv) < 3:
            print("Error: Specify a password to remove. (Usage: gopass rm <name>)", file=sys.stderr)
            sys.exit(1)

        secret_name = sys.argv[2]
        target = get_target_path(gpg_store, secret_name)

        if not os.path.isfile(target):
            print(f"Error: Password '{secret_name}' does not exist.", file=sys.stderr)
            sys.exit(1)

        # Enforce safety prompt confirmation
        try:
            confirm = input(f"Are you sure you want to delete '{secret_name}'? [y/N]: ").strip().lower()
            if confirm in ['y', 'yes']:
                os.remove(target)
                print(f"Removed secret: {secret_name}")
                _git_commit(gpg_store, f"Remove {secret_name}")

                parent_dir = os.path.dirname(target)
                while parent_dir != gpg_store:
                    if not os.listdir(parent_dir):
                        os.rmdir(parent_dir)
                        parent_dir = os.path.dirname(parent_dir)
                    else:
                        break
            else:
                print("Aborted. Secret was not deleted.")
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

    # --- COMMAND: GIT ---
    elif command == "git":
        git_args = sys.argv[2:]
        if not git_args:
            print("Error: Specify a git subcommand. (Usage: gopass git <subcommand>)", file=sys.stderr)
            sys.exit(1)
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
            pull = subprocess.run(["git", "pull", "--rebase"], cwd=gpg_store)
            if pull.returncode != 0:
                print("Error: Pull failed. Resolve conflicts manually.", file=sys.stderr)
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
