#!/usr/bin/env python3
"""
tools/changelog_server.py
Servidor local QTS Changelog — abre http://localhost:7000

APIs:
  GET  /api/ping         → {"ok": true}
  GET  /api/status       → {"staged": [...], "unstaged": [...], "untracked": [...]}
  GET  /api/diff         → {"diff": "...git diff --staged output..."}
  POST /api/commit       → body: {"title": "...", "body": "..."} → {"ok": true, "output": "..."}
  POST /api/commit-notes → body: {"hash": "...", "note": "..."} → saves to storage/changelog_notes.json
  GET  /api/tasks        → lee storage/changelog_tasks.json
  POST /api/tasks        → escribe storage/changelog_tasks.json
  POST /api/regen        → regenera index.html

Uso: python3 tools/changelog_server.py
Abre automáticamente http://localhost:7000 en el navegador.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

PORT = 7000
ROOT = Path(__file__).parent.parent
TASKS_FILE = ROOT / "storage" / "changelog_tasks.json"
NOTES_FILE = ROOT / "storage" / "changelog_notes.json"
INDEX_FILE = ROOT / "index.html"

# Añadir ROOT al path para poder importar desde core/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Intentar usar la DB del sistema; si falla, usar JSON como fallback
try:
    from core.db import get_changelog_tasks as _db_get_tasks
    from core.db import save_changelog_tasks as _db_save_tasks
    _USE_DB = True
except Exception:
    _USE_DB = False


def _get_tasks() -> list:
    if _USE_DB:
        try:
            return _db_get_tasks()
        except Exception:
            pass
    # Fallback JSON
    if TASKS_FILE.exists():
        try:
            return json.loads(TASKS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return []


def _save_tasks(tasks: list) -> None:
    if _USE_DB:
        try:
            _db_save_tasks(tasks)
            return
        except Exception:
            pass
    # Fallback JSON
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASKS_FILE.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Only log non-GET API calls so the terminal isn't noisy
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return
        print(f"  [{self.command}] {path}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str = "text/html; charset=utf-8"):
        if not path.exists():
            self._send_json({"error": "file not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/" or path == "/index.html":
            self._send_file(INDEX_FILE)
            return

        if path == "/api/ping":
            self._send_json({"ok": True})
            return

        if path == "/api/status":
            self._handle_status()
            return

        if path == "/api/diff":
            result = subprocess.run(
                ["git", "diff", "--staged"],
                capture_output=True, text=True, cwd=ROOT
            )
            self._send_json({"diff": result.stdout[:50000]})
            return

        if path == "/api/tasks":
            self._send_json(_get_tasks())
            return

        if path == "/api/commit-notes":
            if NOTES_FILE.exists():
                try:
                    self._send_json(json.loads(NOTES_FILE.read_text("utf-8")))
                except Exception:
                    self._send_json({})
            else:
                self._send_json({})
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(body_raw)
        except Exception:
            body = {}

        path = self.path.split("?")[0]

        if path == "/api/commit":
            self._handle_commit(body)
            return

        if path == "/api/tasks":
            try:
                _save_tasks(body if isinstance(body, list) else [])
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/commit-notes":
            self._handle_commit_notes(body)
            return

        if path == "/api/regen":
            self._handle_regen()
            return

        self._send_json({"error": "not found"}, 404)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _handle_status(self):
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=ROOT
        )
        staged: list[dict] = []
        unstaged: list[dict] = []
        untracked: list[dict] = []

        for line in result.stdout.splitlines():
            if len(line) < 3:
                continue
            xy = line[:2]
            fname = line[3:].strip()
            # Handle renames: "old -> new"
            if " -> " in fname:
                fname = fname.split(" -> ")[-1]

            x = xy[0]  # staged status
            y = xy[1]  # unstaged status

            if xy == "??":
                untracked.append({"file": fname, "status": "?"})
                continue

            if x != " " and x != "?":
                staged.append({"file": fname, "status": x})
            if y != " " and y != "?":
                unstaged.append({"file": fname, "status": y})

        self._send_json({
            "staged":    staged,
            "unstaged":  unstaged,
            "untracked": untracked,
        })

    def _handle_commit(self, body: dict):
        title = body.get("title", "").strip()
        if not title:
            self._send_json({"ok": False, "error": "Título requerido"}, 400)
            return

        msg = title
        if body.get("body", "").strip():
            msg += f"\n\n{body['body'].strip()}"

        result = subprocess.run(
            ["git", "commit", "-m", msg],
            capture_output=True, text=True, cwd=ROOT
        )
        if result.returncode == 0:
            self._send_json({"ok": True, "output": result.stdout})
        else:
            stderr = result.stderr.strip() or result.stdout.strip()
            self._send_json({"ok": False, "error": stderr})

    def _handle_commit_notes(self, body: dict):
        hash_val = body.get("hash", "").strip()
        note     = body.get("note", "").strip()
        if not hash_val:
            self._send_json({"ok": False, "error": "hash requerido"}, 400)
            return
        try:
            NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
            notes = {}
            if NOTES_FILE.exists():
                try:
                    notes = json.loads(NOTES_FILE.read_text("utf-8"))
                except Exception:
                    notes = {}
            notes[hash_val] = note
            NOTES_FILE.write_text(
                json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    def _handle_regen(self):
        venv_python = ROOT / ".venv" / "bin" / "python"
        python_cmd = str(venv_python) if venv_python.exists() else "python3"
        result = subprocess.run(
            [python_cmd, "tools/generate_changelog.py"],
            capture_output=True, text=True, cwd=ROOT
        )
        output = (result.stdout + result.stderr).strip()
        self._send_json({
            "ok": result.returncode == 0,
            "output": output
        })


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def start_background(open_browser: bool = True) -> None:
    """
    Inicia el servidor changelog en un hilo daemon.
    No lanza excepciones al caller — fallo silencioso si el puerto ya está ocupado.
    """
    def _run():
        try:
            server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
            lan_ip = _get_lan_ip()
            print(f"[QTS Changelog] http://localhost:{PORT}  (LAN: http://{lan_ip}:{PORT})")
            if open_browser:
                threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
            server.serve_forever()
        except OSError:
            pass  # Puerto ya en uso — otro proceso ya tiene el servidor
        except Exception as e:
            print(f"[QTS Changelog] Error en servidor: {e}")

    t = threading.Thread(target=_run, daemon=True, name="ChangelogServer")
    t.start()


def main():
    lan_ip = _get_lan_ip()
    print(f"[QTS Changelog] Servidor iniciado en http://localhost:{PORT}  (LAN: http://{lan_ip}:{PORT})")
    print(f"[QTS Changelog] Ctrl+C para detener")
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[QTS Changelog] Servidor detenido.")
        server.server_close()


if __name__ == "__main__":
    main()
