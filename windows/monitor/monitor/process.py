"""Own the named pipe and the business child lifecycle."""
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from multiprocessing.connection import Listener
from pathlib import Path


class BusinessProcess:
    def __init__(self, target_url=''):
        # 传给子进程的 TargetPageGuard：离开目标页时子进程自己退出。
        self.target_url = target_url
        self.child = None
        self.connection = None
        self.events = queue.SimpleQueue()
        self.listener = None
        self.desired = False
        self.stopping_at = None

    def start(self):
        if self.child is not None:
            return
        self.desired = True
        name = r'\\.\pipe\FxxkPuzzleMonitor-' + uuid.uuid4().hex
        auth = os.urandom(32)
        self.listener = Listener(name, family='AF_PIPE', authkey=auth)
        if getattr(sys, 'frozen', False):
            command = [str(Path(sys.executable).with_name('Fxxk_Puzzle.exe'))]
            working_directory = None
        else:
            working_directory = Path(__file__).resolve().parents[1]
            command = [sys.executable, '-m', 'app.business']
        args = ['--pipe', name, '--auth', auth.hex()]
        if self.target_url:
            args += ['--target-url', self.target_url]
        self.child = subprocess.Popen(command + args,
                                      creationflags=subprocess.CREATE_NO_WINDOW,
                                      cwd=working_directory)
        threading.Thread(target=self._connect, args=(self.listener,), daemon=True).start()

    def _connect(self, listener):
        connection = None
        try:
            connection = listener.accept()
            self.connection = connection
            if not self.desired:
                connection.send('EXIT')
            while True:
                self.events.put(connection.recv())
        except (EOFError, OSError):
            self.events.put('DISCONNECTED')
        finally:
            if connection is not None:
                connection.close()
            if self.connection is connection:
                self.connection = None
            listener.close()
            if self.listener is listener:
                self.listener = None

    def send(self, command):
        if self.connection:
            try:
                self.connection.send(command)
                return True
            except OSError:
                pass
        return False

    def stop(self):
        self.desired = False
        self.send('EXIT')
        self.stopping_at = time.monotonic()

    def poll(self):
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                break
        if self.child and self.child.poll() is not None:
            if self.listener:
                self.listener.close()
                self.listener = None
            self.child = None
            self.connection = None
            self.stopping_at = None
            result.append('EXITED')
        elif self.child and self.stopping_at and time.monotonic() - self.stopping_at > 5:
            self.child.terminate()
        return result
