import base64
import copy
import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

import paramiko

DEFAULT_PORT = 22
MIN_PORT = 1
MAX_PORT = 65535

_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:\-%\[\]]{1,253}$")
_RECORD_MARKERS = ("[record]", "[/record]")


class NotConnectedError(RuntimeError):
    pass


class CommandTimeoutError(TimeoutError):
    pass


class HostKeyMismatchError(paramiko.SSHException):
    """The key presented for host:port differs from the one saved in known_hosts.

    Typical benign cause: a different device (different MAC) now answers on the
    same IP. The UI can ask the user and retry with replace_changed_key=True.
    """

    def __init__(self, message, host="", port=DEFAULT_PORT, fingerprint="", saved=()):
        super().__init__(message)
        self.host = host
        self.port = port
        self.fingerprint = fingerprint
        self.saved = tuple(saved)


class _RestrictedNegotiationError(Exception):
    """Handshake failed while limited to the stored key types (internal)."""


# ── validation ────────────────────────────────────────────────────────────────
def helper_validate_host(host):
    cleaned = str(host).strip()
    if not _HOST_PATTERN.match(cleaned):
        raise ValueError("Invalid host/IP. Use only letters, digits, '.', '-' and ':'.")
    return cleaned


def helper_validate_port(value):
    text = str(value).strip()
    if not text:
        return DEFAULT_PORT
    try:
        port = int(text)
    except ValueError:
        raise ValueError("Port must be a number.") from None
    if port < MIN_PORT or port > MAX_PORT:
        raise ValueError(f"Port must be between {MIN_PORT} and {MAX_PORT}.")
    return port


def helper_validate_username(user, max_length):
    cleaned = str(user).strip()
    if not cleaned:
        raise ValueError("Username is required.")
    if len(cleaned) > int(max_length):
        raise ValueError("Username is too long.")
    if any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
        raise ValueError("Username must not contain whitespace or control characters.")
    return cleaned


def helper_validate_command(command, max_length):
    cleaned = str(command).strip()
    if not cleaned:
        raise ValueError("Command is empty.")
    if len(cleaned) > int(max_length):
        raise ValueError(f"Command is longer than {int(max_length)} characters.")
    if "\x00" in cleaned:
        raise ValueError("Command contains a NUL character.")
    return cleaned


def helper_parse_interval(value, limits):
    low = int(limits["min_interval_s"])
    high = int(limits["max_interval_s"])
    try:
        interval = int(str(value).strip())
    except (TypeError, ValueError):
        return int(limits["default_interval_s"])
    return max(low, min(high, interval))


# ── record format (the dashboard parses exactly this) ─────────────────────────
def helper_now_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _helper_single_line(text):
    return str(text).replace("\r", " ").replace("\n", " ")


def helper_sanitize_output(text):
    cleaned = str(text).replace("\x00", "")
    safe_lines = []
    for line in cleaned.splitlines():
        if line.strip() in _RECORD_MARKERS:
            line = "\\" + line
        safe_lines.append(line)
    return "\n".join(safe_lines)


def helper_format_record(timestamp, host, command, output, tag=""):
    tag_line = f"tag={_helper_single_line(tag)}\n" if tag else ""
    return (
        "[record]\n"
        f"time={timestamp}\n"
        f"host={_helper_single_line(host)}\n"
        f"cmd={_helper_single_line(command)}\n"
        f"{tag_line}"
        "---\n"
        f"{helper_sanitize_output(output).rstrip()}\n"
        "[/record]\n\n"
    )


def helper_key_fingerprint(key):
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _helper_write_private_file(path, writer):
    temp_path = path + ".tmp"
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def helper_open_folder(folder):
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Folder does not exist: {folder}")
    if sys.platform.startswith("win"):
        os.startfile(folder)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])


# ── profiles ──────────────────────────────────────────────────────────────────
class ProfileStore:
    def __init__(self, path, max_name_length):
        self._path = path
        self._max_name_length = int(max_name_length)
        self._data = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("Profiles file must contain a JSON object.")
            self._data = loaded
        except (OSError, ValueError):
            self._data = {}
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                os.replace(self._path, f"{self._path}.corrupt-{stamp}")
            except OSError:
                pass

    def _save(self):
        _helper_write_private_file(self._path, lambda handle: json.dump(self._data, handle, indent=2))

    def _clean_name(self, name):
        if not isinstance(name, str):
            raise ValueError("Profile name must be text.")
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Profile name cannot be empty.")
        if len(cleaned) > self._max_name_length:
            raise ValueError(f"Profile name is longer than {self._max_name_length} characters.")
        return cleaned

    def names(self):
        with self._lock:
            return sorted(self._data)

    def save(self, name, profile):
        cleaned_name = self._clean_name(name)
        if not isinstance(profile, dict):
            raise ValueError("Profile data must be a dictionary.")
        with self._lock:
            self._data[cleaned_name] = copy.deepcopy(profile)
            self._save()
        return cleaned_name

    def load(self, name):
        with self._lock:
            profile = self._data.get(name, {})
            return copy.deepcopy(profile) if isinstance(profile, dict) else {}

    def delete(self, name):
        with self._lock:
            self._data.pop(name, None)
            self._save()


# ── host key pinning ──────────────────────────────────────────────────────────
class KnownHostsStore:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._keys = paramiko.HostKeys()
        if os.path.exists(path):
            try:
                self._keys.load(path)
            except (OSError, paramiko.SSHException):
                self._keys = paramiko.HostKeys()

    @staticmethod
    def helper_entry_name(host, port):
        return host if port == DEFAULT_PORT else f"[{host}]:{port}"

    def known_key_types(self, host, port):
        with self._lock:
            entry = self._keys.lookup(self.helper_entry_name(host, port))
            return [] if entry is None else list(entry.keys())

    def get_key(self, host, port, key_type):
        with self._lock:
            entry = self._keys.lookup(self.helper_entry_name(host, port))
            return None if entry is None else entry.get(key_type)

    def saved_fingerprints(self, host, port):
        with self._lock:
            entry = self._keys.lookup(self.helper_entry_name(host, port))
            if entry is None:
                return []
            return [f"{name} {helper_key_fingerprint(key)}" for name, key in entry.items()]

    def add(self, host, port, key):
        with self._lock:
            self._keys.add(self.helper_entry_name(host, port), key.get_name(), key)
            self._save_locked()

    def replace(self, host, port, key):
        """Drop every saved key of host:port and trust `key` instead."""
        name = self.helper_entry_name(host, port)
        with self._lock:
            try:
                del self._keys[name]
            except KeyError:
                pass
            self._keys.add(name, key.get_name(), key)
            self._save_locked()

    def _save_locked(self):
        temp_path = self.path + ".tmp"
        self._keys.save(temp_path)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, self.path)


# ── SSH ───────────────────────────────────────────────────────────────────────
class SSHManager:
    def __init__(self, known_hosts, cfg):
        self._ssh_cfg = cfg["ssh"]
        self._max_command = int(cfg["limits"]["max_command"])
        self._max_username = int(cfg["limits"]["max_username"])
        self._known_hosts = known_hosts

        self._transport = None
        self._lock = threading.Lock()
        self._exec_lock = threading.Lock()
        self._connect_lock = threading.Lock()

        self._host = ""
        self._port = DEFAULT_PORT
        self._user = ""
        self._password = ""
        self._key_path = None

        self.last_fingerprint = ""
        self.host_key_was_new = False
        self.host_key_was_replaced = False

    def connect(self, host, port, user, password, key_path=None, replace_changed_key=False):
        clean_host = helper_validate_host(host)
        clean_port = helper_validate_port(port)
        clean_user = helper_validate_username(user, self._max_username)

        with self._connect_lock:
            transport = self._open_transport(clean_host, clean_port, clean_user, password, key_path,
                                             replace_changed_key)
            self._install_transport(transport)
            with self._lock:
                self._host = clean_host
                self._port = clean_port
                self._user = clean_user
                self._password = password
                self._key_path = key_path

    def reconnect(self, replace_changed_key=False):
        with self._connect_lock:
            with self._lock:
                host, port, user = self._host, self._port, self._user
                password, key_path = self._password, self._key_path

            if not host or not user:
                raise NotConnectedError("No stored credentials to reconnect with.")

            self._close_current_transport()
            time.sleep(float(self._ssh_cfg["reconnect_pause_s"]))
            self._install_transport(self._open_transport(host, port, user, password, key_path,
                                                         replace_changed_key))

    def disconnect(self):
        self._close_current_transport()

    def _close_current_transport(self):
        with self._lock:
            old_transport, self._transport = self._transport, None
        self._helper_safe_close(old_transport)

    def _install_transport(self, new_transport):
        with self._lock:
            old_transport, self._transport = self._transport, new_transport
        self._helper_safe_close(old_transport)

    @staticmethod
    def _helper_safe_close(transport):
        if transport is None:
            return
        try:
            transport.close()
        except Exception:
            pass

    def _open_transport(self, host, port, user, password, key_path, replace_changed_key=False):
        # Pin the handshake to the key types already saved for this address, so a
        # downgrade to an unknown key type is refused. If the device now answering
        # offers none of them (a different device on the same IP), the handshake
        # fails; retry unrestricted so host-key verification reports the change
        # (HostKeyMismatchError) instead of a cryptic negotiation error.
        restrict = (not replace_changed_key) and bool(self._known_hosts.known_key_types(host, port))
        if restrict:
            try:
                return self._connect_once(host, port, user, password, key_path, True, False)
            except _RestrictedNegotiationError:
                pass
        return self._connect_once(host, port, user, password, key_path, False, replace_changed_key)

    def _connect_once(self, host, port, user, password, key_path, restrict, replace_changed_key):
        timeout = int(self._ssh_cfg["connect_timeout"])
        sock = socket.create_connection((host, port), timeout=timeout)
        transport = None
        try:
            transport = paramiko.Transport(sock)
            transport.banner_timeout = timeout
            transport.auth_timeout = timeout

            if restrict:
                self._restrict_key_types(transport, host, port)
            try:
                transport.start_client(timeout=timeout)
            except paramiko.SSHException:
                if restrict:
                    raise _RestrictedNegotiationError() from None
                raise
            self._verify_host_key(transport, host, port, replace_changed_key)
            self._authenticate(transport, user, password, key_path)

            if not transport.is_authenticated():
                raise paramiko.AuthenticationException("Authentication failed.")

            transport.set_keepalive(int(self._ssh_cfg["keepalive_s"]))
            return transport
        except Exception:
            if transport is not None:
                self._helper_safe_close(transport)
            else:
                sock.close()
            raise

    def _restrict_key_types(self, transport, host, port):
        known_types = self._known_hosts.known_key_types(host, port)
        if not known_types:
            return
        options = transport.get_security_options()
        allowed = [t for t in options.key_types if t in known_types]
        if allowed:
            options.key_types = allowed

    def _verify_host_key(self, transport, host, port, replace_changed_key=False):
        remote_key = transport.get_remote_server_key()
        fingerprint = helper_key_fingerprint(remote_key)
        stored_key = self._known_hosts.get_key(host, port, remote_key.get_name())
        host_is_known = bool(self._known_hosts.known_key_types(host, port))

        self.host_key_was_new = False
        self.host_key_was_replaced = False

        if stored_key is None and not host_is_known:
            self._known_hosts.add(host, port, remote_key)
            self.host_key_was_new = True
        elif stored_key is None or stored_key.asbytes() != remote_key.asbytes():
            if not replace_changed_key:
                raise HostKeyMismatchError(
                    f"HOST KEY CHANGED for {host}:{port}.\n"
                    f"Presented: {fingerprint}\n"
                    "A different device may now be using this address, or the server was "
                    "reinstalled. It could also be a man-in-the-middle attack.\n"
                    f"Saved keys are in:\n{self._known_hosts.path}",
                    host=host, port=port, fingerprint=fingerprint,
                    saved=self._known_hosts.saved_fingerprints(host, port))
            self._known_hosts.replace(host, port, remote_key)
            self.host_key_was_replaced = True

        self.last_fingerprint = fingerprint

    def _authenticate(self, transport, user, password, key_path):
        if key_path:
            if not os.path.isfile(key_path):
                raise FileNotFoundError(f"SSH key file not found: {key_path}")
            transport.auth_publickey(user, self._helper_load_private_key(key_path, password))
            return
        transport.auth_password(user, password)

    @staticmethod
    def _helper_load_private_key(key_path, passphrase):
        key_classes = [getattr(paramiko, name) for name in ("RSAKey", "Ed25519Key", "ECDSAKey")
                       if hasattr(paramiko, name)]
        for key_class in key_classes:
            try:
                return key_class.from_private_key_file(key_path, password=passphrase or None)
            except paramiko.PasswordRequiredException:
                raise ValueError(
                    "The SSH key is encrypted. Enter its passphrase in the Password field."
                ) from None
            except (paramiko.SSHException, ValueError):
                continue
        raise ValueError(f"Cannot load private key: {key_path}")

    def execute(self, command):
        clean_command = helper_validate_command(command, self._max_command)
        timeout = int(self._ssh_cfg["command_timeout"])

        with self._exec_lock:
            with self._lock:
                transport = self._transport
            if transport is None or not transport.is_active():
                raise NotConnectedError("Not connected")

            channel = transport.open_session(timeout=timeout)
            try:
                channel.exec_command(clean_command)
                return self._collect_output(channel, transport, timeout)
            finally:
                channel.close()

    @staticmethod
    def _helper_append_limited(buffer, chunk, limit):
        room = limit - len(buffer)
        if room <= 0:
            return True
        if len(chunk) > room:
            buffer.extend(chunk[:room])
            return True
        buffer.extend(chunk)
        return False

    def _collect_output(self, channel, transport, timeout):
        chunk_size = int(self._ssh_cfg["read_chunk"])
        poll_sleep = float(self._ssh_cfg["poll_sleep_s"])
        limit = int(self._ssh_cfg["max_output_bytes"])
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        truncated = False
        deadline = time.monotonic() + timeout

        while True:
            if time.monotonic() >= deadline:
                raise CommandTimeoutError(f"Command timed out after {timeout} seconds.")

            if channel.recv_ready():
                truncated |= self._helper_append_limited(stdout_buffer, channel.recv(chunk_size), limit)
                continue

            if channel.recv_stderr_ready():
                truncated |= self._helper_append_limited(
                    stderr_buffer, channel.recv_stderr(chunk_size), limit)
                continue

            if channel.exit_status_ready() or channel.closed:
                if not (channel.recv_ready() or channel.recv_stderr_ready()):
                    break
                continue

            if not transport.is_active():
                raise ConnectionError("Connection lost while running command.")

            time.sleep(poll_sleep)

        stdout_text = stdout_buffer.decode(errors="replace")
        stderr_text = stderr_buffer.decode(errors="replace")
        if truncated:
            stdout_text += f"\n[output truncated at {limit} bytes per stream]"
        return stdout_text, stderr_text

    @property
    def connected(self):
        with self._lock:
            transport = self._transport
        return transport is not None and transport.is_active()

    @property
    def host(self):
        with self._lock:
            return self._host

    @property
    def has_credentials(self):
        with self._lock:
            return bool(self._host and self._user)


# ── logging ───────────────────────────────────────────────────────────────────
class Logger:
    def __init__(self, filepath, host):
        descriptor = os.open(filepath, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._fp = os.fdopen(descriptor, "a", encoding="utf-8", errors="replace")
        self._host = host
        self._lock = threading.Lock()
        self._closed = False

    def write(self, command, output, tag=""):
        block = helper_format_record(helper_now_timestamp(), self._host, command, output, tag)
        with self._lock:
            if self._closed:
                return block
            self._fp.write(block)
            self._fp.flush()
        return block

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._fp.close()
            except Exception:
                pass


class CommandRunner(threading.Thread):
    def __init__(self, command, interval, tag, ssh, logger, event_queue):
        super().__init__(daemon=True, name="CommandRunner")
        self.command = command
        self.interval = interval
        self.tag = tag
        self.ssh = ssh
        self.logger = logger
        self.event_queue = event_queue
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            self._run_cycle()
            self._stop_event.wait(self.interval)

    def _run_cycle(self):
        try:
            combined = helper_run_command(self.ssh, self.command)
        except Exception as exc:
            combined = f"[ERROR] {exc}"
            self.event_queue.put(("error", str(exc)))

        if self._stop_event.is_set():
            return

        try:
            block = self.logger.write(self.command, combined, tag=self.tag)
        except OSError as exc:
            self.event_queue.put(("error", f"Log write failed: {exc}"))
            return

        self.event_queue.put(("record", block))

    def stop(self):
        self._stop_event.set()


def helper_run_command(ssh, command):
    stdout, stderr = ssh.execute(command)
    if stderr.strip():
        return stdout + "\n[stderr]\n" + stderr
    return stdout


def helper_tail_read_lines(filepath, line_count, chunk_bytes):
    if line_count <= 0:
        return [], 0

    with open(filepath, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        file_size = handle.tell()

        collected = b""
        position = file_size
        newline_count = 0

        while position > 0 and newline_count <= line_count:
            read_size = min(int(chunk_bytes), position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            collected = chunk + collected
            newline_count += chunk.count(b"\n")

    lines = collected.decode("utf-8", errors="replace").splitlines()
    return lines[-line_count:], file_size
