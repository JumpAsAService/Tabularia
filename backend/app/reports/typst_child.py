"""The Typst compile of one render, in a process of its own (started by app/reports/pdf.py).

Typst's caches grow by about 130 MB per render in a living process, so every render runs
here and the memory goes back to the system when it ends. This file imports nothing of
Tabularia (it starts with `python -I`, isolated): it limits itself first, then reads the
job as JSON on stdin and answers as JSON on stdout.

    python -I typst_child.py MEMORY_MB PARENT_PID < job.json

- RLIMIT_DATA at MEMORY_MB: what the process really allocates (RLIMIT_AS would count the
  address space Typst's threads reserve and fail at twice the real use);
- it dies with its parent (PR_SET_PDEATHSIG): a render cancelled by killing the worker's
  process takes this one with it.

The job: {"root", "fonts", "main", "inputs", "format": pdf|svg|none, "output",
"hotspots": bool, "pngs": [{"src", "w", "h", "output"}]}. The fonts are only those of
`fonts`: no system font, no font Typst carries.
"""
import ctypes
import json
import os
import resource
import signal
import sys


def _limit(memory_mb: int, parent: int) -> None:
    limit = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_DATA, (limit, limit))
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
    except OSError:  # not glibc: the timeout of the parent still holds
        pass
    if os.getppid() != parent:  # the parent died before the line above
        os._exit(1)


def _pages(compiler) -> int:
    return int(json.loads(compiler.query("<pages>", field="value", one=True, format="json")))


def main() -> dict:
    job = json.loads(sys.stdin.read())
    import typst

    fonts = typst.Fonts(include_system_fonts=False, include_embedded_fonts=False, font_paths=[job["fonts"]])
    root = job["root"]
    out: dict = {"ok": True}
    try:
        if job.get("main"):
            compiler = typst.Compiler(os.path.join(root, job["main"]), root=root, font_paths=fonts,
                                      sys_inputs=job["inputs"])
            if job["format"] == "pdf":
                pdf = compiler.compile(format="pdf", timestamp=0)
                with open(job["output"], "wb") as f:
                    f.write(pdf)
            elif job["format"] == "svg":
                pages = compiler.compile(format="svg")
                pages = pages if isinstance(pages, list) else [pages]
                result = {"pages": [p.decode("utf-8") for p in pages]}
                if job.get("hotspots"):
                    result["hotspots"] = json.loads(compiler.query("<hotspots>", field="value", one=True,
                                                                   format="json"))
                with open(job["output"], "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False)
            out["pages"] = _pages(compiler)
        for png in job.get("pngs") or []:
            source = (f'#set page(width: {png["w"] * 0.75}pt, height: {png["h"] * 0.75}pt, margin: 0pt, fill: white)\n'
                      '#image(sys.inputs.src, width: 100%, height: 100%)')
            data = typst.compile(source.encode(), root=root, font_paths=fonts, sys_inputs={"src": png["src"]},
                                 format="png", ppi=192)
            with open(png["output"], "wb") as f:
                f.write(data if isinstance(data, bytes) else data[0])
    except MemoryError:
        return {"ok": False, "error": "memory"}
    except Exception as e:  # noqa: BLE001 — typst.TypstError and the like: told to the parent
        message = str(getattr(e, "message", "") or e)
        if "tabularia:pages:" in message:
            return {"ok": False, "error": "pages", "pages": int(message.split("tabularia:pages:")[1].split('"')[0])}
        return {"ok": False, "error": "typst", "detail": message[:1000]}
    return out


if __name__ == "__main__":
    _limit(int(sys.argv[1]), int(sys.argv[2]))
    result = main()
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
