"""Start the scheduled server in this process; Windows handles subsequent triggers."""
from datetime import datetime
import os
from pathlib import Path
import socket
import sys


def other_home_running(main_path):
    import psutil

    own = psutil.Process()
    skip = {own.pid, *(p.pid for p in own.parents())}  # includes the venv launcher
    for process in psutil.process_iter(["name"]):
        if process.pid in skip or (process.info["name"] or "").lower() not in (
            "python.exe", "pythonw.exe", "uvicorn.exe"
        ):
            continue
        try:
            args = process.cmdline()[1:]
            for arg in args:
                if Path(arg).name.lower() == "main.py":
                    if (Path(process.cwd()) / arg).resolve() == main_path:
                        return True
            if "main:app" in args and Path(process.cwd()).resolve() == main_path.parent:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return False


def run(main_path):
    data = main_path.parent / "data"
    data.mkdir(exist_ok=True)

    def record(result):
        with (data / "home-recovery.log").open("a", encoding="utf-8") as log:
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} pid={os.getpid()} {result}", file=log)

    # Claim the port before importing the app or starting any background workers.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("0.0.0.0", 8080))
        except OSError as error:
            if error.winerror == 10048:  # another listener already owns this port
                return
            raise
        if other_home_running(main_path):
            return
        # pythonw has no console streams. Keep normal console Python for CLI children.
        sys.executable = str(Path(sys.executable).with_name("python.exe"))
        for stream in ("stdout", "stderr"):
            path = data / f"home-restart.{stream}.log"
            if path.exists():
                path.replace(data / f"home-restart.previous.{stream}.log")
            setattr(sys, stream, path.open("w", encoding="utf-8", buffering=1, errors="backslashreplace"))

        try:
            import uvicorn

            class HomeServer(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets=sockets)
                    if self.started:
                        record("ready")

            server = HomeServer(uvicorn.Config("main:app", host="0.0.0.0", port=8080))
            server.run(sockets=[listener])
            if not server.started:
                raise RuntimeError("Home server failed to start")
        except BaseException as error:
            record(f"failed: {type(error).__name__}: {error}")
            raise
