# SftpSync

SftpSync is a Neovim plugin that helps you sync your projects to an SFTP
server.

## Install

This plugin is known to work with Neovim stable. Version **0.5.0+** is
recommended. It may still work with older releases.

Install SftpSync using your favorite plugin manager. Using vim-plug:

```vim
Plug 'dcampos/nvim-sftp-sync', { 'do': ':UpdateRemotePlugins' }
```

This plugin has an external dependence on the `pynvim` and `paramiko` Python packages. I recommend using `uv` to create a virtual environment and then adding `vim.g.python3_host_prog = ~/.local/share/nvim/.venv/bin/python`.

```
cd ~/.local/share/nvim
uv venv
source .venv/bin/activate
uv pip install pynvim paramiko
```

## Usage

Basic server configuration:

```
let g:sftp_sync_servers = {
            \     'server1': {
            \         'local_path': '/home/myuser/projects/project1',
            \         'remote_path': '/server/project1',
            \         'host': 'myserver.com',
            \         'username': 'mysftpuser',
            \         'password': 's3cret',
            \         # 'private_key': '/home/myuser/.ssh/id_xxx',
            \     }
            \ }
```

Send the currently open file:

```
:SftpSend
```

Receive the currently open file:
```
:SftpRecv
```

See `:help sftp-sync` for more details.

## License

MIT license.
