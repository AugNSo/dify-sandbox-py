import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO


def main() -> None:
    code_path = sys.argv[1]
    with open(code_path, "r", encoding="utf-8") as f:
        code = f.read()

    stdout_buffer = StringIO()
    stderr_buffer = StringIO()
    try:
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            exec(compile(code, "<sandbox>", "exec"), {})
        result = {
            "success": True,
            "output": stdout_buffer.getvalue(),
            "error": stderr_buffer.getvalue() or None,
        }
    except Exception as e:
        result = {
            "success": False,
            "output": stdout_buffer.getvalue(),
            "error": str(e),
        }

    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
