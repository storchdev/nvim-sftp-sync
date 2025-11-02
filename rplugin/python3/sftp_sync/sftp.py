import logging
import socket
import time
from pathlib import Path, PurePosixPath
from threading import Timer

import paramiko
from pynvim import Nvim


class SyncStatus:
    NONE = -1
    OK = 0
    SENDING = 1
    ERROR = 2
    RECEIVING = 3  # distinct status for download operations


callers = {}


def debounce(wait, call_id, func):
    def call_func():
        try:
            callers.pop(call_id)
            func()
        except KeyError:
            pass

    try:
        caller = callers[call_id]
        caller.cancel()
    except KeyError:
        pass

    caller = Timer(wait, call_func)
    caller.start()
    callers[call_id] = caller


class SftpClient:
    WAIT_TIME = 0.25

    def __init__(self, nvim: Nvim, servers: dict):
        self.vim = nvim
        self.servers = dict(sorted(servers.items()))
        # pool[server] = {"ssh": SSHClient, "sftp": SFTPClient}
        self.pool = {}
        self.runners = {}
        self.logger = logging.getLogger("SFTP_SYNC")
        self.selected_server = None

    # ===== Public API =====

    # Upload (local -> remote)
    def send(self, file) -> None:
        selected_server = self._pick_server_for_file(file)
        _, _, _, remote_abs = self._paths_for(file, selected_server)

        call_id = f"PUT:{file}:{remote_abs}:{selected_server}"
        self.logger.debug(call_id)

        debounce(
            self.WAIT_TIME,
            call_id,
            lambda: self.vim.async_call(
                self._do_send, file, remote_abs, selected_server
            ),
        )

    # Download (remote -> local)
    def recv(self, file) -> None:
        selected_server = self._pick_server_for_file(file)
        _, _, file_path, remote_abs = self._paths_for(file, selected_server)

        call_id = f"GET:{file}:{remote_abs}:{selected_server}"
        self.logger.debug(call_id)

        debounce(
            self.WAIT_TIME,
            call_id,
            lambda: self.vim.async_call(
                self._do_recv, str(file_path), remote_abs, selected_server
            ),
        )

    # ===== Internal workers =====

    def _do_send(self, file, destination, selected_server):
        self.vim.async_call(lambda: self._set_status(file, SyncStatus.SENDING))
        start = time.time()

        try:
            sftp = self._connect(selected_server)
        except Exception as e:
            self.logger.error(e, exc_info=True)
            result, msg = False, f"Error connecting to {selected_server}: {repr(e)}"
            self.vim.async_call(lambda: self._update_results(file, result, msg, None))
            return

        result, msg = True, f"{selected_server} -> OK (uploaded)"

        try:
            self.logger.debug("Sending file %s to %s", file, destination)
            # Ensure remote dir exists
            remote_dir = PurePosixPath(destination).parent.as_posix()
            self._ensure_remote_dir(sftp, remote_dir)
            sftp.put(file, destination)
        except (socket.timeout, paramiko.SSHException, IOError, OSError) as e:
            self.logger.error(e, exc_info=True)
            # if it's a timeout, nuke pool (like before)
            if isinstance(e, socket.timeout):
                result, msg = False, f"Timeout error: {repr(e)}"
                self.vim.async_call(self.reset)
                self.reset()
            else:
                result, msg = False, f"Upload error: {repr(e)}"

        elapsed = time.time() - start if result else None
        self.vim.async_call(lambda: self._update_results(file, result, msg, elapsed))

    def _do_recv(self, local_file, remote_abs, selected_server):
        self.vim.async_call(lambda: self._set_status(local_file, SyncStatus.RECEIVING))
        start = time.time()

        try:
            sftp = self._connect(selected_server)
        except Exception as e:
            self.logger.error(e, exc_info=True)
            result, msg = False, f"Error connecting to {selected_server}: {repr(e)}"
            self.vim.async_call(
                lambda: self._update_results(local_file, result, msg, None)
            )
            return

        result, msg = True, f"{selected_server} -> OK (downloaded)"

        try:
            self.logger.debug("Receiving file %s to %s", remote_abs, local_file)
            Path(local_file).parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_abs, local_file)
        except (socket.timeout, paramiko.SSHException) as e:
            self.logger.error(e, exc_info=True)
            result, msg = False, f"Timeout/SSH error: {repr(e)}"
            self.vim.async_call(self.reset)
            self.reset()
        except FileNotFoundError as e:
            self.logger.error(e, exc_info=True)
            result, msg = False, f"Local path error: {repr(e)}"
        except (IOError, OSError) as e:
            self.logger.error(e, exc_info=True)
            result, msg = False, f"Remote path error (does it exist?): {repr(e)}"

        elapsed = time.time() - start if result else None
        self.vim.async_call(
            lambda: self._update_results(local_file, result, msg, elapsed)
        )
        self.vim.async_call(lambda: self._reload_buffer(local_file))

    # ===== Helpers =====

    def _pick_server_for_file(self, file: str) -> str:
        if self.selected_server is None:
            for name, server in self.servers.items():
                self.logger.debug(f"server: {server.get('local_path')}")
                # Requires Python 3.9+; replace with try/except relative_to for older Pythons
                if Path(file).is_relative_to(server["local_path"]):
                    selected_server = name
                    self.logger.debug(f"selected server: {server}")
                    break
            else:
                raise Exception("No server selected for the current path")
        else:
            selected_server = self.selected_server

        self.logger.debug("Selected server: %s", selected_server)
        return selected_server

    def _paths_for(self, file: str, selected_server: str):
        server = self.servers[selected_server]
        local_path = Path(server["local_path"])
        file_path = Path(file)
        relative_path = file_path.relative_to(local_path)
        remote_path = PurePosixPath(server["remote_path"])
        remote_abs = (remote_path / relative_path).as_posix()
        return server, local_path, file_path, remote_abs

    def _update_results(self, file, result, msg, elapsed):
        self._set_status(file, SyncStatus.OK if result else SyncStatus.ERROR)
        if result:
            if elapsed is not None:
                self.vim.out_write(f"[SFTP] -> {msg} ({elapsed:.2f}s)\n")
            else:
                self.vim.out_write(f"[SFTP] -> {msg}\n")
        else:
            self.vim.err_write(f"[SFTP] -> {msg}\n")

    def _set_status(self, file, status):
        bufnr = self.vim.funcs.bufnr(file)
        buffer = self.vim.buffers[bufnr]
        buffer.vars["sftp_sync_status"] = status
        self.vim.command("doautocmd User SftpStatusChanged")

    # --- Connection management (paramiko) ---

    def _connect(self, server_name: str):
        # Return a live SFTPClient; create and pool if missing
        try:
            pooled = self.pool[server_name]
            return pooled["sftp"]
        except KeyError:
            pass

        self.logger.debug("Creating connection to %s", server_name)
        cfg = self.servers[server_name]
        host = cfg["host"]
        port = cfg.get("port", 22)
        username = cfg.get("username")
        password = cfg.get("password")
        key_filename = cfg.get("private_key")  # path to key file
        passphrase = cfg.get("private_key_pass")  # passphrase for key (if any)

        ssh = paramiko.SSHClient()
        # Trust on first use (TOFU); optionally replace with known_hosts if you prefer stricter policy
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # You can tweak these timeouts if desired
        connect_kwargs = dict(
            hostname=host,
            port=port,
            username=username,
            password=password,
            key_filename=key_filename,
            passphrase=passphrase,
            look_for_keys=False if key_filename or password else True,
            allow_agent=True,
            timeout=30,  # socket connect timeout
            banner_timeout=30,  # banner
            auth_timeout=30,  # auth
        )

        ssh.connect(**connect_kwargs)

        # Keepalive like before
        transport = ssh.get_transport()
        if transport is not None:
            transport.set_keepalive(60)

        sftp = ssh.open_sftp()
        self.pool[server_name] = {"ssh": ssh, "sftp": sftp}
        return sftp

    def _ensure_remote_dir(self, sftp: paramiko.SFTPClient, remote_dir: str) -> None:
        """
        A simple 'mkdir -p' for remote directories using SFTP.
        """
        path = PurePosixPath(remote_dir)

        # Build chain from root -> leaf to create in order
        chain = []
        cur = path
        while True:
            chain.append(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
        chain.reverse()

        for part in chain:
            p = part.as_posix()
            if p == "":
                continue
            try:
                sftp.stat(p)
            except IOError:
                try:
                    sftp.mkdir(p)
                except IOError:
                    # Might have been created by a race; ignore
                    pass

    def _reload_buffer(self, file):
        """Reload buffer if it's already open in Neovim."""
        bufnr = self.vim.funcs.bufnr(file)
        if bufnr != -1:
            # Use checktime to trigger file reload if changed externally
            try:
                self.vim.command(f"checktime {file}")
            except Exception:
                # Fallback: force reload silently
                self.vim.command(f"edit! {file}")

    def reset(self):
        self.quit()
        for buffer in self.vim.buffers:
            buffer.vars["sftp_sync_status"] = SyncStatus.NONE
        self.pool = {}

    def quit(self):
        for pooled in self.pool.values():
            try:
                pooled["sftp"].close()
            except Exception:
                pass
            try:
                pooled["ssh"].close()
            except Exception:
                pass

    def keepalive(self):
        for pooled in self.pool.values():
            try:
                pooled["sftp"].listdir(".")
            except Exception:
                pass
