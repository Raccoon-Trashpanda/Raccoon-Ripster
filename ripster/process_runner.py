"""
Process runner — a single well-behaved subprocess wrapper used by the queue.

Why this exists:
  Before, the queue inlined ``asyncio.create_subprocess_exec`` with its own
  timeout logic, ANSI stripping, shutdown handling, and engine dispatch.
  Stopping a task was done by grabbing a ``global active_proc`` and calling
  ``.terminate()`` from somewhere else — that broke as soon as more than one
  concurrent task was on the table.

  This class takes the pieces and makes them explicit:
    * Run a command, stream lines as structured ``Event`` objects.
    * Per-line read timeout (configurable) to catch hung processes.
    * Escalating shutdown: cancel → terminate → wait(5s) → kill → wait.
    * Proper lifecycle — the process is owned by the runner, no globals.
    * Windows CREATE_NO_WINDOW flag so we don't spawn console popups.

Not implemented (yet):
  * Resource limits (memory/CPU). Needs platform-specific code.
  * stdin-based communication (AMD's prompt_toolkit REPL).
    That lives in ``run_task_amd`` and stays there for now — once we have
    a second reason to pipe stdin, unify them.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from ripster.engines.base import EngineBase, Event, EventKind, LineLevel


IS_WINDOWS = sys.platform.startswith("win")


def _ensure_home_env(env: dict) -> None:
    """Guarantee a resolvable home dir in the child env.

    streamrip does ``HOME = Path.home()`` at import time and crashes with
    "Could not determine home directory" when USERPROFILE/HOME are absent — which
    happens because the PyQt/detached launcher can start the server process
    without those vars. gamdl/yt-dlp/others also expand ``~``. Reconstruct the
    home path from whatever is available so every engine subprocess has it."""
    home = (env.get("USERPROFILE") or env.get("HOME")
            or ((env.get("HOMEDRIVE", "") + env.get("HOMEPATH", "")) or "")
            or os.path.expanduser("~"))
    if not home or home == "~":
        un = env.get("USERNAME") or os.environ.get("USERNAME") or "Default"
        home = f"C:\\Users\\{un}" if IS_WINDOWS else f"/home/{un}"
    env.setdefault("USERPROFILE", home)
    env.setdefault("HOME", home)
    if IS_WINDOWS and (not env.get("HOMEDRIVE") or not env.get("HOMEPATH")):
        drive, path = os.path.splitdrive(home)
        if drive:
            env.setdefault("HOMEDRIVE", drive)
            env.setdefault("HOMEPATH", path or "\\")
    if IS_WINDOWS:
        env.setdefault("APPDATA", os.path.join(home, "AppData", "Roaming"))
        env.setdefault("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))

# Read timeout per line. If the subprocess doesn't emit any output for this
# long, we treat it as hung and terminate. Engines sometimes legitimately go
# quiet for a while (network stall, long FFmpeg transcode), so this is
# deliberately generous — 5 minutes is "something is wrong" territory.
DEFAULT_LINE_TIMEOUT = 300.0


def _smart_decode(raw: bytes) -> str:
    """Decode subprocess output bytes to str.

    Tries UTF-8 first (strict). If any bytes are invalid UTF-8, falls back to
    CP1251 (Windows Cyrillic) and then to Latin-1 (lossless single-byte
    fallback). This handles tools like streamrip/deemix on Russian Windows
    that emit CP1251 without PYTHONIOENCODING being set.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1251")
    except UnicodeDecodeError:
        pass
    return raw.decode("latin-1")


@dataclass
class RunResult:
    """Summary of a completed run.

    ``exit_code`` is ``None`` if we gave up waiting and killed the process.
    ``timed_out`` is True iff the per-line read timeout fired.
    """
    exit_code: Optional[int]
    timed_out: bool
    log_lines: list[str]


class ProcessRunner:
    """Run an external command and stream its output as ``Event``s.

    Usage:
        runner = ProcessRunner(cmd=["rip","url","..."], engine=some_engine)
        async for ev in runner.run():
            ... handle ev ...
        print(runner.result.exit_code)

    Cancellation: call ``await runner.cancel()`` from another task.
    This will terminate the subprocess and the ``run()`` generator will exit
    cleanly on its next iteration.
    """

    def __init__(
        self,
        cmd: list[str],
        engine: EngineBase,
        *,
        env: Optional[dict] = None,
        cwd: Optional[str] = None,
        line_timeout: float = DEFAULT_LINE_TIMEOUT,
        shutdown_grace: float = 5.0,
        use_thread: bool = False,
    ) -> None:
        self.cmd = cmd
        self.engine = engine
        self.env = env
        self.cwd = cwd
        self.line_timeout = line_timeout
        self.shutdown_grace = shutdown_grace
        # When True, spawn via a blocking subprocess.Popen + reader thread instead
        # of asyncio.create_subprocess_exec. Needed for child processes that are
        # themselves complex asyncio/gRPC apps (AMD): under an asyncio parent on
        # Windows they exit early, but a plain blocking Popen runs them correctly.
        self.use_thread = use_thread

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._popen: Optional[subprocess.Popen] = None
        self._cancelled = False
        self.result: Optional[RunResult] = None

    async def run(self) -> AsyncIterator[Event]:
        """Launch the subprocess and yield Events until it exits or we cancel.

        Events come from the engine's ``iter_events`` (v2 API) — the default
        base impl bridges the old v1 methods, so legacy engines keep working.
        """
        env = {**os.environ, **(self.env or {})}
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("NO_COLOR", "1")   # hint to CLIs that support it
        env.setdefault("TERM", "dumb")
        _ensure_home_env(env)

        if self.use_thread:
            async for ev in self._run_threaded(env):
                yield ev
            return

        flags: dict = {}
        if IS_WINDOWS:
            # Don't pop a console window for each subprocess on Windows.
            flags["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        log_lines: list[str] = []

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=self.cwd,
                # Progressbar tools (progressbar/v3) use \r to update in-place.
                # Between two \n characters the output can accumulate to hundreds of KB.
                # Raise the StreamReader limit so we never crash with LimitOverrunError.
                limit=8 * 1024 * 1024,
                **flags,
            )
        except FileNotFoundError as e:
            # Binary missing — surface as a FATAL event, don't raise. Callers
            # rarely want to distinguish "binary missing" from other failures
            # at the top level; they want a clean error message to show.
            yield Event(
                kind=EventKind.FATAL,
                message=f"Не найден исполняемый файл: {self.cmd[0]} ({e})",
                level=LineLevel.ERROR,
            )
            self.result = RunResult(exit_code=127, timed_out=False, log_lines=[])
            return

        timed_out = False
        progress = (0, 0)

        try:
            # Read loop. We iterate until EOF or cancellation.
            while True:
                if self._cancelled:
                    break
                try:
                    raw = await asyncio.wait_for(
                        self._proc.stdout.readline(),
                        timeout=self.line_timeout,
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    yield Event(
                        kind=EventKind.LINE,
                        message=f"⚠ Нет вывода {int(self.line_timeout)} секунд — процесс завис?",
                        level=LineLevel.WARN,
                    )
                    break

                if not raw:
                    break   # EOF

                decoded = _smart_decode(raw)
                # Tools like schollz/progressbar use \r (not \n) to update a progress
                # line in-place.  readline() buffers everything until \n, so hundreds
                # of \r-updates accumulate as one huge "line".  Split on \r and keep
                # only the last non-empty segment — the final visible state.
                cr_parts = [p.rstrip("\n") for p in decoded.split("\r")]
                line = next((p for p in reversed(cr_parts) if p.strip()), "")
                if not line:
                    continue
                log_lines.append(line)

                # Let the engine turn this line into zero or more events.
                # The engine is responsible for ANSI stripping (default impl
                # does it in the bridge).
                for ev in self.engine.iter_events(line, progress=progress):
                    if ev.kind is EventKind.PROGRESS:
                        # Remember progress for the next line's call.
                        if ev.current is not None: progress = (ev.current, progress[1])
                        if ev.total   is not None: progress = (progress[0], ev.total)
                    yield ev

            # Wait for process to actually exit, but not forever.
            await self._shutdown()

        finally:
            # Always record a finished RunResult, even on exceptions.
            exit_code = (
                self._proc.returncode
                if self._proc and self._proc.returncode is not None
                else None
            )
            self.result = RunResult(
                exit_code=exit_code,
                timed_out=timed_out,
                log_lines=log_lines,
            )

    async def _run_threaded(self, env: dict) -> AsyncIterator[Event]:
        """Blocking ``subprocess.Popen`` + reader-thread variant of ``run``.

        For engines whose child is itself a complex asyncio/gRPC app (AMD):
        spawning them via ``asyncio.create_subprocess_exec`` makes them exit
        early on Windows (the child's own event loop / handles misbehave under
        an async parent). A plain blocking ``Popen`` runs them exactly like the
        CLI does; a daemon thread pumps stdout into an asyncio queue so we still
        stream Events without blocking the event loop.
        """
        flags: dict = {}
        if IS_WINDOWS:
            flags["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        log_lines: list[str] = []
        timed_out = False
        progress = (0, 0)
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        try:
            self._popen = subprocess.Popen(
                self.cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=self.cwd,
                bufsize=0,
                **flags,
            )
        except FileNotFoundError as e:
            yield Event(kind=EventKind.FATAL,
                        message=f"Не найден исполняемый файл: {self.cmd[0]} ({e})",
                        level=LineLevel.ERROR)
            self.result = RunResult(exit_code=127, timed_out=False, log_lines=[])
            return

        def _reader() -> None:
            try:
                for raw in iter(self._popen.stdout.readline, b""):
                    loop.call_soon_threadsafe(q.put_nowait, raw)
            except Exception:
                pass
            finally:
                loop.call_soon_threadsafe(q.put_nowait, _SENTINEL)

        threading.Thread(target=_reader, daemon=True).start()

        try:
            while True:
                if self._cancelled:
                    break
                try:
                    raw = await asyncio.wait_for(q.get(), timeout=self.line_timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    yield Event(kind=EventKind.LINE,
                                message=f"⚠ Нет вывода {int(self.line_timeout)} секунд — процесс завис?",
                                level=LineLevel.WARN)
                    break
                if raw is _SENTINEL:
                    break
                decoded = _smart_decode(raw)
                cr_parts = [p.rstrip("\n") for p in decoded.split("\r")]
                line = next((p for p in reversed(cr_parts) if p.strip()), "")
                if not line:
                    continue
                log_lines.append(line)
                for ev in self.engine.iter_events(line, progress=progress):
                    if ev.kind is EventKind.PROGRESS:
                        if ev.current is not None: progress = (ev.current, progress[1])
                        if ev.total   is not None: progress = (progress[0], ev.total)
                    yield ev
        finally:
            if self._popen and self._popen.poll() is None:
                try:
                    self._popen.terminate()
                except Exception:
                    pass
                try:
                    await loop.run_in_executor(None, lambda: self._popen.wait(self.shutdown_grace))
                except Exception:
                    try:
                        self._popen.kill()
                    except Exception:
                        pass
            rc = self._popen.returncode if self._popen else None
            self.result = RunResult(exit_code=rc, timed_out=timed_out, log_lines=log_lines)

    async def cancel(self) -> None:
        """Request graceful shutdown. Safe to call multiple times."""
        self._cancelled = True
        if self._proc is not None:
            await self._shutdown()
        if self._popen is not None and self._popen.poll() is None:
            try:
                self._popen.terminate()
            except Exception:
                pass

    async def _shutdown(self) -> None:
        """Escalating shutdown: terminate → wait(grace) → kill → wait.

        Idempotent: if the process is already gone, does nothing. Swallows
        ProcessLookupError which happens when the process exited between
        our check and our signal.
        """
        if self._proc is None or self._proc.returncode is not None:
            return

        try:
            self._proc.terminate()
        except ProcessLookupError:
            return  # already gone
        except Exception as e:
            print(f"[runner] terminate failed: {e}", file=sys.stderr, flush=True)

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=self.shutdown_grace)
            return   # graceful exit
        except asyncio.TimeoutError:
            pass

        # Escalation: SIGKILL.
        print(
            f"[runner] subprocess did not exit within {self.shutdown_grace}s; "
            f"sending SIGKILL",
            file=sys.stderr, flush=True,
        )
        try:
            self._proc.kill()
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            # At this point the OS is failing us; log and move on.
            print("[runner] subprocess still alive after SIGKILL — giving up",
                  file=sys.stderr, flush=True)
