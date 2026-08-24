import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

_RUNNER_PATH = str(Path(__file__).with_name("python_runner.py"))


def _pdeathsig() -> None:
    """Kill this process if its parent dies (e.g. API process crash)."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:
        pass


def _apply_memory_limit(max_memory_bytes: int | None) -> None:
    if not max_memory_bytes:
        return
    try:
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes)
        )
    except Exception:
        pass


def _job_preexec(max_memory_bytes: int | None = None):
    def _inner() -> None:
        _pdeathsig()
        _apply_memory_limit(max_memory_bytes)

    return _inner


def _kill_process_group(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def check_nodejs_available() -> bool:
    try:
        subprocess.run(
            ["node", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


class CodeExecutor:
    def __init__(
        self,
        timeout: int = 30,
        max_memory_mb: int | None = None,
    ):
        self.timeout = timeout
        self.max_memory_bytes = (
            int(max_memory_mb) * 1024 * 1024 if max_memory_mb else None
        )
        self.nodejs_available = check_nodejs_available()
        self._procs: set[asyncio.subprocess.Process] = set()
        self._lock = asyncio.Lock()

    async def shutdown(self) -> None:
        async with self._lock:
            procs = list(self._procs)
        for proc in procs:
            if proc.returncode is None:
                _kill_process_group(proc.pid)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass

    async def _track(self, proc: asyncio.subprocess.Process) -> None:
        async with self._lock:
            self._procs.add(proc)

    async def _untrack(self, proc: asyncio.subprocess.Process) -> None:
        async with self._lock:
            self._procs.discard(proc)

    async def _run_job(
        self,
        args: list,
        timeout_message: str,
    ) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_job_preexec(self.max_memory_bytes),
        )
        await self._track(proc)
        timed_out = False
        try:
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                timed_out = True
                _kill_process_group(proc.pid)
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=1
                    )
                except asyncio.TimeoutError:
                    stdout_b, stderr_b = b"", b""
        finally:
            if proc.returncode is None:
                _kill_process_group(proc.pid)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
            await self._untrack(proc)

        if timed_out:
            return {
                "success": False,
                "output": (stdout_b or b"").decode("utf-8", errors="replace"),
                "error": timeout_message,
            }

        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        return {
            "success": proc.returncode == 0,
            "output": stdout,
            "error": None if proc.returncode == 0 else stderr,
            "returncode": proc.returncode,
        }

    async def _run_python(self, code: str) -> dict[str, Any]:
        code_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(code)
                code_path = f.name

            raw = await self._run_job(
                [sys.executable, _RUNNER_PATH, code_path],
                f"代码执行超时 (>{self.timeout}秒)",
            )
            if not raw["success"] and raw.get("error", "").startswith(
                "代码执行超时"
            ):
                return {
                    "success": False,
                    "output": "",
                    "error": raw["error"],
                }

            # Runner prints a JSON result on stdout; preserve prior API shape.
            if raw.get("returncode") == 0 or raw["output"]:
                try:
                    result = json.loads(raw["output"] or "")
                    return {
                        "success": bool(result.get("success")),
                        "output": result.get("output") or "",
                        "error": result.get("error"),
                    }
                except json.JSONDecodeError:
                    if raw["success"]:
                        return {
                            "success": False,
                            "output": raw["output"],
                            "error": "Invalid runner response",
                        }

            return {
                "success": False,
                "output": raw["output"] or "",
                "error": raw["error"]
                or f"Python exited with code {raw.get('returncode')}",
            }
        except Exception as e:
            return {"success": False, "output": "", "error": str(e)}
        finally:
            if code_path is not None:
                try:
                    os.unlink(code_path)
                except OSError:
                    pass

    async def _run_nodejs(self, code: str) -> dict[str, Any]:
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".js",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(code)
                path = f.name

            raw = await self._run_job(
                ["node", path],
                f"Node.js execution timed out (>{self.timeout}s)",
            )
            return {
                "success": raw["success"],
                "output": raw["output"] or "",
                "error": raw["error"],
            }
        except Exception as e:
            return {"success": False, "output": "", "error": str(e)}
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def execute(self, code: str, language: str = "python3") -> dict[str, Any]:
        if language == "python3":
            return await self._run_python(code)
        if language == "nodejs":
            if not self.nodejs_available:
                return {
                    "success": False,
                    "output": "",
                    "error": "Node.js not available",
                }
            return await self._run_nodejs(code)
        return {
            "success": False,
            "output": "",
            "error": f"Unsupported language: {language}",
        }
