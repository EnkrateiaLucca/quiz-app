#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["segno"]
# ///
"""Local server for the quiz app.

Serves quiz-app.html plus quiz JSON / media files, and exposes a small API:

  GET  /api/ping                  -> {"app": "quiz-server"}
  GET  /api/quizzes               -> list of quiz JSON files available
  GET  /api/quiz?file=<name>      -> contents of a quiz JSON
  GET  /api/phone?file=&q=        -> {"url", "svg"} LAN link + QR to resume on a phone
  GET  /localfile?path=<abspath>  -> serve a media file by absolute path (home dir only)
  POST /api/anki                  -> {front, back, source} add a card to the Anki DB
  POST /api/quiz/delete-question  -> {file, index, question} remove a question from the JSON

Anki insertion tries AnkiConnect (instant, silent) and falls back to the
anki-card-creator skill script (.apkg + auto-import) when Anki isn't running
with the add-on.

Usage:
  uv run quiz_server.py                # start server + open the app
  uv run quiz_server.py my-quiz.json   # start server + open with that quiz loaded
"""

import io
import json
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import segno
except ImportError:  # QR generation degrades gracefully
    segno = None

# Per-run capability token. Loopback (the local browser) is trusted and exempt;
# any non-loopback client (a phone on the LAN) must present this token, which is
# baked into the QR URL. Keeps the "continue on phone" feature usable without
# leaving the sensitive routes open to every host on the network.
TOKEN = secrets.token_urlsafe(18)

# Routes that let a caller read home-dir files, mutate data, or spend compute —
# gated by TOKEN for non-loopback clients. Quiz listing/content and the QR
# endpoint stay open (that content is what you're handing to your own phone).
GUARDED_ROUTES = ("/localfile", "/api/anki", "/api/ai/grade",
                  "/api/quiz/delete-question", "/api/phone")

_LAN_IP_CACHE = None

ROOT = Path(__file__).resolve().parent
PORT = 8321
APP_URL = f"http://127.0.0.1:{PORT}/quiz-app.html"

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_DECK = "Knowledge"          # matches DECK_CONFIG in create-anki-cards.py
ANKI_MODEL = "Basic"             # user's Basic note type: Front, Back, Source
ANKI_SKILL_SCRIPT = Path.home() / ".claude/skills/anki-card-creator/scripts/create-anki-cards.py"

OLLAMA_URL = "http://127.0.0.1:11434"
AI_DEFAULT_MODEL = "gemma3:12b"  # 5/5 on the grading benchmark, ~1.5s warm
AI_KEEP_ALIVE = "15m"            # keep the grader loaded between questions

GRADE_SYSTEM = (
    "You grade quiz answers. Judge whether the user's answer is semantically "
    "correct even if worded differently from the accepted answers. Accept "
    "synonyms, paraphrases, translations, and minor typos when the meaning is "
    "right. Reject answers that are wrong, incomplete on the key point, or "
    "grammatically incorrect when the question asks for a correct form. "
    "Reply with JSON: verdict (correct | partially_correct | incorrect) and "
    "feedback (one short sentence, max 25 words)."
)

GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "partially_correct", "incorrect"]},
        "feedback": {"type": "string"},
    },
    "required": ["verdict", "feedback"],
}

MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
              ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac", ".mp4"}

MIME = {
    ".html": "text/html; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".js": "text/javascript", ".css": "text/css", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".opus": "audio/opus", ".aac": "audio/aac", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".txt": "text/plain; charset=utf-8",
}

# Quiz files opened from outside ROOT (via CLI arg), keyed by basename.
REGISTERED: dict[str, Path] = {}


def resolve_quiz_path(name: str) -> Path | None:
    name = Path(name).name  # basename only, no traversal
    if name in REGISTERED and REGISTERED[name].exists():
        return REGISTERED[name]
    candidate = ROOT / name
    if candidate.exists():
        return candidate
    return None


# ---------------------------------------------------------------- Anki

def anki_connect(action: str, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(ANKI_CONNECT_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode())
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data.get("result")


def add_card(front: str, back: str, source: str) -> dict:
    # 1) AnkiConnect: adds directly to the collection, no dialogs.
    try:
        note = {
            "deckName": ANKI_DECK,
            "modelName": ANKI_MODEL,
            "fields": {"Front": front, "Back": back, "Source": source},
            "options": {"allowDuplicate": False, "duplicateScope": "deck"},
        }
        note_id = anki_connect("addNote", note=note)
        return {"ok": True, "method": "ankiconnect",
                "message": f"Added to '{ANKI_DECK}' deck (note {note_id})."}
    except RuntimeError as e:
        if "duplicate" in str(e).lower():
            return {"ok": False, "method": "ankiconnect",
                    "message": "Already in Anki (duplicate)."}
        # other AnkiConnect errors (missing deck/model) fall through to the script
    except (urllib.error.URLError, OSError, TimeoutError):
        pass  # Anki not running / add-on missing -> fall back to skill script

    # 2) Fallback: anki-card-creator skill script (.apkg + auto-import via `open`).
    if not ANKI_SKILL_SCRIPT.exists():
        return {"ok": False, "method": "none",
                "message": "Anki is not running (AnkiConnect unreachable) and the "
                           "anki-card-creator script was not found."}
    try:
        result = subprocess.run(
            ["uv", "run", str(ANKI_SKILL_SCRIPT),
             "-q", front, "-a", back, "-s", source, "-c", "knowledge"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0:
            return {"ok": True, "method": "apkg",
                    "message": "Anki wasn't running — card packaged and sent to Anki "
                               "for import (confirm the import dialog in Anki)."}
        return {"ok": False, "method": "apkg",
                "message": f"Card script failed: {result.stderr.strip()[:300]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "method": "apkg", "message": f"Card script error: {e}"}


# ---------------------------------------------------------------- Ollama grading

def ollama_json(path: str, payload: dict | None = None, timeout: int = 240):
    url = OLLAMA_URL + path
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def list_ai_models() -> dict:
    try:
        tags = ollama_json("/api/tags", timeout=4)
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"online": False, "models": [], "default": AI_DEFAULT_MODEL}
    names = [m["name"] for m in tags.get("models", []) if "embed" not in m["name"].lower()]
    default = AI_DEFAULT_MODEL if AI_DEFAULT_MODEL in names else (names[0] if names else "")
    return {"online": True, "models": sorted(names), "default": default}


def grade_answer(question: str, accepted: list, explanation: str,
                 user_answer: str, model: str) -> dict:
    payload = {
        "model": model or AI_DEFAULT_MODEL,
        "stream": False,
        "format": GRADE_SCHEMA,
        "options": {"temperature": 0, "num_predict": 200},
        "keep_alive": AI_KEEP_ALIVE,
        "think": False,  # thinking models (qwen3.5) return empty content otherwise
        "messages": [
            {"role": "system", "content": GRADE_SYSTEM},
            {"role": "user", "content": json.dumps({
                "question": question,
                "accepted_answers": accepted,
                "explanation": explanation or None,
                "user_answer": user_answer,
            }, ensure_ascii=False)},
        ],
    }
    t0 = time.time()
    try:
        try:
            data = ollama_json("/api/chat", payload)
        except urllib.error.HTTPError as e:
            if e.code == 400:  # model rejects the `think` field -> retry without it
                payload.pop("think", None)
                data = ollama_json("/api/chat", payload)
            else:
                raise
        result = json.loads(data["message"]["content"])
        if result.get("verdict") not in ("correct", "partially_correct", "incorrect"):
            raise ValueError("bad verdict")
        return {"ok": True, "verdict": result["verdict"],
                "feedback": result.get("feedback", ""),
                "model": payload["model"], "ms": int((time.time() - t0) * 1000)}
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"ok": False, "message": "Ollama is not reachable on 127.0.0.1:11434 — "
                                        "start it with `ollama serve` (or open the Ollama app)."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Grading failed on {payload['model']}: {e}"}


# ---------------------------------------------------------------- quiz editing

def delete_question(name: str, index: int, question_text: str) -> dict:
    path = resolve_quiz_path(name)
    if path is None:
        return {"ok": False, "message": f"Quiz file '{name}' not found on disk."}
    quiz = json.loads(path.read_text())
    if not isinstance(quiz, list):
        return {"ok": False, "message": "Quiz file is not a JSON array."}

    if not (0 <= index < len(quiz) and quiz[index].get("question") == question_text):
        # index drifted (e.g. earlier deletions) - find by exact question text
        matches = [i for i, q in enumerate(quiz) if q.get("question") == question_text]
        if len(matches) != 1:
            return {"ok": False, "message": "Question not found in the quiz file "
                                            "(it may have been edited elsewhere)."}
        index = matches[0]

    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    removed = quiz.pop(index)
    path.write_text(json.dumps(quiz, indent=2, ensure_ascii=False) + "\n")
    return {"ok": True, "remaining": len(quiz),
            "message": f"Deleted from {path.name} ({len(quiz)} questions left). "
                       f"Backup: {path.name}.bak",
            "removed": removed.get("question")}


# ---------------------------------------------------------------- continue on phone

def lan_ip() -> str:
    """Best-effort LAN IP this machine is reachable at (no packets are sent)."""
    global _LAN_IP_CACHE
    if _LAN_IP_CACHE is not None:
        return _LAN_IP_CACHE
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # picks the outbound interface; connectionless
        _LAN_IP_CACHE = s.getsockname()[0]
    except OSError:
        _LAN_IP_CACHE = "127.0.0.1"
    finally:
        s.close()
    return _LAN_IP_CACHE


def host_allowed(host_header: str) -> bool:
    """Reject requests whose Host isn't loopback or this machine's LAN IP.

    Blocks DNS-rebinding: a malicious website that resolves its name to this
    machine's IP still sends its own Host header, which won't match."""
    if not host_header:
        return False
    hostname = host_header.rsplit(":", 1)[0].strip("[]")  # drop port / IPv6 brackets
    return hostname in {"127.0.0.1", "localhost", "::1", lan_ip()}


def phone_payload(name: str, q: str) -> dict:
    ip = lan_ip()
    query = "token=" + urllib.parse.quote(TOKEN)  # phone is non-loopback -> needs the token
    frag = ""
    if name:
        frag = "#quizfile=" + urllib.parse.quote(Path(name).name) + "&autostart=1"
        if q.isdigit():
            frag += "&q=" + q
    url = f"http://{ip}:{PORT}/quiz-app.html?{query}{frag}"
    if segno is None:
        return {"ok": False, "url": url, "ip": ip, "svg": None,
                "message": "QR unavailable — `segno` isn't installed."}
    buf = io.BytesIO()
    segno.make(url, error="m").save(
        buf, kind="svg", xmldecl=False, scale=4, border=2,
        dark="#1A1813", light="#FCFBF7",
    )
    return {"ok": True, "url": url, "ip": ip, "svg": buf.getvalue().decode()}


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("[quiz-server] %s\n" % (fmt % args))

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- access control ------------------------------------------------
    def _client_is_local(self) -> bool:
        return self.client_address and self.client_address[0] in ("127.0.0.1", "::1")

    def _guard(self, route: str, params: dict) -> bool:
        """Enforce the Host allowlist (all routes) and the token (guarded routes,
        non-loopback callers). Sends the error response and returns False on deny."""
        if not host_allowed(self.headers.get("Host", "")):
            self.send_error(403, "host not allowed")
            return False
        if route in GUARDED_ROUTES and not self._client_is_local():
            token = params.get("token", [None])[0] or self.headers.get("X-Quiz-Token")
            if not token or not secrets.compare_digest(token, TOKEN):
                self.send_error(403, "token required")
                return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        if not self._guard(route, params):
            return

        if route == "/api/ping":
            return self._json({"app": "quiz-server"})

        if route == "/api/ai/models":
            return self._json(list_ai_models())

        if route == "/api/quizzes":
            files = sorted(p.name for p in ROOT.glob("*.json"))
            files += [n for n in REGISTERED if n not in files]
            return self._json({"quizzes": sorted(set(files))})

        if route == "/api/quiz":
            name = params.get("file", [""])[0]
            path = resolve_quiz_path(name)
            if path is None:
                return self._json({"error": "not found"}, 404)
            return self._file(path)

        if route == "/api/phone":
            return self._json(phone_payload(
                params.get("file", [""])[0], params.get("q", [""])[0]))

        if route == "/localfile":
            raw = params.get("path", [""])[0]
            p = Path(raw)
            try:
                p = p.expanduser().resolve()
            except OSError:
                return self.send_error(400)
            if not (p.is_file() and p.suffix.lower() in MEDIA_EXTS
                    and p.is_relative_to(Path.home())):
                return self.send_error(403)
            return self._file(p)

        # static files from ROOT
        if route == "/":
            route = "/quiz-app.html"
        target = (ROOT / route.lstrip("/")).resolve()
        if not (target.is_relative_to(ROOT) and target.is_file()):
            return self.send_error(404)
        return self._file(target)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        if not self._guard(route, params):
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "message": "invalid JSON"}, 400)

        if route == "/api/ai/grade":
            question = (body.get("question") or "").strip()
            answer = (body.get("userAnswer") or "").strip()
            if not question or not answer:
                return self._json({"ok": False, "message": "question and userAnswer required"}, 400)
            return self._json(grade_answer(
                question,
                body.get("acceptedAnswers") or [],
                body.get("explanation") or "",
                answer,
                (body.get("model") or "").strip(),
            ))

        if route == "/api/anki":
            front = (body.get("front") or "").strip()
            back = (body.get("back") or "").strip()
            source = (body.get("source") or "").strip()
            if not front or not back:
                return self._json({"ok": False, "message": "front and back required"}, 400)
            return self._json(add_card(front, back, source))

        if route == "/api/quiz/delete-question":
            name = body.get("file") or ""
            index = body.get("index")
            question = body.get("question") or ""
            if not name or index is None or not question:
                return self._json({"ok": False, "message": "file, index and question required"}, 400)
            result = delete_question(name, int(index), question)
            return self._json(result, 200 if result["ok"] else 409)

        return self._json({"ok": False, "message": "unknown endpoint"}, 404)


# ---------------------------------------------------------------- main

def server_already_running() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1) as r:
            return json.loads(r.read().decode()).get("app") == "quiz-server"
    except Exception:  # noqa: BLE001
        return False


def open_browser(url: str):
    subprocess.run(["open", url], check=False)


def main():
    quiz_arg = sys.argv[1] if len(sys.argv) > 1 else None
    url = APP_URL
    if quiz_arg:
        qpath = Path(quiz_arg).expanduser().resolve()
        if not qpath.is_file():
            sys.exit(f"quiz file not found: {qpath}")
        REGISTERED[qpath.name] = qpath
        url = f"{APP_URL}?t={int(time.time())}#quizfile={urllib.parse.quote(qpath.name)}&autostart=1"

    if server_already_running():
        # NOTE: an already-running server won't know about a newly registered
        # external file unless it lives in ROOT. Copy it in so /api/quiz finds it.
        if quiz_arg:
            qpath = REGISTERED[Path(quiz_arg).name]
            if not qpath.is_relative_to(ROOT):
                shutil.copy2(qpath, ROOT / qpath.name)
        print(f"quiz-server already running on port {PORT} — opening browser.")
        open_browser(url)
        return

    try:
        # bind on all interfaces so a phone on the same Wi-Fi can reach it (QR
        # "continue on phone"); loopback still works for the local browser.
        httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        sys.exit(f"port {PORT} is in use by another process")

    threading.Timer(0.4, open_browser, args=(url,)).start()
    print(f"quiz-server: http://127.0.0.1:{PORT}  (LAN: http://{lan_ip()}:{PORT})  "
          f"(root: {ROOT})  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
