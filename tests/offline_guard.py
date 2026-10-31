"""Process-wide guards for the isolated test runner (including Python children)."""
import os
import socket
import subprocess


def enable():
    if getattr(socket, '_zotero_offline', False):
        return

    def deny_connection(*args, **kwargs):
        raise OSError('Offline tests cannot open network connections')

    socket.socket.connect = deny_connection
    socket.socket.connect_ex = deny_connection
    socket.create_connection = deny_connection
    # Native libcurl bypasses Python sockets; block its client entry point too.
    try:
        from curl_cffi.requests import Session, AsyncSession
        Session.request = deny_connection
        AsyncSession.request = deny_connection
    except (ImportError, OSError):
        pass
    real_popen = subprocess.Popen

    def guarded_popen(args, *positional, **kwargs):
        # Only local test helpers and local Git fixtures may start processes.
        if kwargs.get('shell') or isinstance(args, (str, bytes)):
            raise OSError('Offline tests require explicit subprocess arguments')
        name = os.path.basename(os.fspath(args[0]))
        allowed = name.startswith('python') or name in ('git', 'pdftotext', 'pdftoppm', 'tesseract')
        remote = any(str(a).startswith(('https://', 'http://', 'ssh://', 'git@')) for a in args[1:])
        if not allowed or (name == 'git' and remote):
            raise OSError(f'External subprocess disabled in offline tests: {name}')
        return real_popen(args, *positional, **kwargs)

    subprocess.Popen = guarded_popen
    socket._zotero_offline = True
