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
import ipaddress
import json
import re
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
GUARDED_ROUTES = ("/localfile", "/api/anki", "/api/ai/grade", "/api/ai/chat",
                  "/api/quiz/delete-question", "/api/quiz/update-question",
                  "/api/quiz/delete", "/api/phone", "/api/article")

_LAN_IP_CACHE = None

ROOT = Path(__file__).resolve().parent
PORT = 8321
APP_URL = f"http://127.0.0.1:{PORT}/quiz-app.html"

ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_DECK = "Knowledge"          # matches DECK_CONFIG in create-anki-cards.py
ANKI_MODEL = "Basic"             # user's Basic note type: Front, Back, Source
ANKI_SKILL_SCRIPT = Path.home() / ".claude/skills/anki-card-creator/scripts/create-anki-cards.py"

OLLAMA_URL = "http://127.0.0.1:11434"
AI_DEFAULT_MODEL = "gemma4:12b-mlx"  # local grader (MLX build, fast on Apple Silicon)
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

# ---------------------------------------------------------------- article proxy
# The /api/article route fetches a source article server-side and returns it
# iframe-ready (frame-blocking headers stripped, same-origin with the app), so the
# split-view pane can render it and the parent can drive scroll/highlight over
# postMessage. ARTICLE_HIGHLIGHTER_JS is injected into every proxied page.
ARTICLE_MAX_BYTES = 5_000_000
ARTICLE_TIMEOUT = 10

ARTICLE_HIGHLIGHTER_JS = r"""
<style>
  mark.quiz-hl { background: #ffe58a; color: #1a1813 !important;
    box-shadow: 0 0 0 3px #ffe58a; border-radius: 2px; scroll-margin: 120px; }
  /* Many sites hide body content until a scroll-reveal script runs; we strip the
     page's own scripts, so force the common JS-gated states visible for a readable
     reader pane. Best-effort — imperfect on heavily client-rendered pages. */
  html, body { opacity: 1 !important; }
  [class*="fade"], [class*="reveal"], [class*="animate"], [class*="inview"],
  [data-animate], [data-aos], [style*="opacity:0"], [style*="opacity: 0"] {
    opacity: 1 !important; visibility: visible !important;
    transform: none !important; filter: none !important;
  }
  /* Page-transition curtains / preloaders are normally removed by the site's JS
     (which we strip), leaving a full-screen overlay covering the article. */
  [class*="transition_wrap"], [class*="page-transition"], [class*="page_transition"],
  [class*="preloader"], [class*="loading-screen"], [class*="loader_wrap"] {
    display: none !important;
  }
</style>
<script>
(function () {
  // Native Chrome text fragments (#:~:text=) don't activate inside an iframe, so
  // the parent posts us a parsed target and we find + highlight it ourselves.
  function clearHl() {
    document.querySelectorAll('mark.quiz-hl').forEach(function (m) {
      var p = m.parentNode;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
  }
  // Concatenate visible text nodes + keep a map back to (node, offset).
  function collectText() {
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
        var t = n.parentNode && n.parentNode.nodeName;
        if (t === 'SCRIPT' || t === 'STYLE' || t === 'NOSCRIPT')
          return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var full = '', map = [], n;
    while ((n = walker.nextNode())) { map.push({ node: n, start: full.length }); full += n.nodeValue; }
    return { full: full, map: map };
  }
  function posAt(map, off) {
    for (var i = map.length - 1; i >= 0; i--) {
      if (map[i].start <= off) return { node: map[i].node, offset: off - map[i].start };
    }
    return null;
  }
  // Escape regex metachars, then make every run of whitespace tolerant (\s+) so a
  // fragment string matches DOM text even when it wraps or splits across inline tags.
  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+'); }
  function findRange(tgt) {
    if (!tgt.textStart) return null;
    var t = collectText();
    var pat = esc(tgt.textStart);
    if (tgt.textEnd) pat += '[\\s\\S]*?' + esc(tgt.textEnd);
    var m;
    try { m = new RegExp(pat).exec(t.full); } catch (e) { return null; }
    if (!m) return null;
    var s = posAt(t.map, m.index), e = posAt(t.map, m.index + m[0].length);
    if (!s || !e) return null;
    var r = document.createRange();
    try { r.setStart(s.node, s.offset); r.setEnd(e.node, e.offset); } catch (err) { return null; }
    return r;
  }
  // Tell the parent whether we actually landed on the passage, so it can fall
  // back to showing the quote inline for a manual ⌘F.
  function report(found) {
    if (window.parent && window.parent !== window)
      window.parent.postMessage({ type: 'quiz-highlight-result', found: !!found }, '*');
  }
  function highlight(tgt) {
    clearHl();
    if (!tgt) return report(false);
    if (tgt.kind === 'anchor') {
      var el = document.getElementById(tgt.id) || document.getElementsByName(tgt.id)[0];
      if (el) el.scrollIntoView({ block: 'center' });
      return report(!!el);
    }
    // full match, else fall back to a textStart-only match (still highlights something)
    var r = findRange(tgt) || (tgt.textEnd ? findRange({ textStart: tgt.textStart }) : null);
    if (!r) return report(false);
    var mk = document.createElement('mark');
    mk.className = 'quiz-hl';
    try { r.surroundContents(mk); }
    catch (e) { mk.appendChild(r.extractContents()); r.insertNode(mk); }
    mk.scrollIntoView({ block: 'center', behavior: 'smooth' });
    report(true);
  }
  // Hide big fixed/absolute opaque overlays that carry no text — page-transition
  // curtains the stripped JS would normally have removed. Heuristic, best-effort.
  function deCurtain() {
    var vw = window.innerWidth, vh = window.innerHeight;
    document.querySelectorAll('body *').forEach(function (el) {
      var s = window.getComputedStyle(el);
      if (s.position !== 'fixed' && s.position !== 'absolute') return;
      var r = el.getBoundingClientRect();
      if (r.width < vw * 0.9 || r.height < vh * 0.9) return;   // must ~cover viewport
      var bg = s.backgroundColor;
      var opaque = bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent'
        && !/rgba\([^)]+,\s*0(\.0+)?\)/.test(bg);
      if (opaque && (el.innerText || '').trim().length < 20) el.style.display = 'none';
    });
  }
  window.addEventListener('message', function (ev) {
    var d = ev.data || {};
    if (d.type === 'quiz-highlight') highlight(d.target);
  });
  window.addEventListener('load', function () {
    try { deCurtain(); } catch (e) {}
    if (window.parent && window.parent !== window)
      window.parent.postMessage({ type: 'quiz-article-ready' }, '*');
  });
})();
</script>
"""


def _host_is_public(host: str) -> bool:
    """True only if every address `host` resolves to is a public/global IP.

    Load-bearing SSRF guard: a hostname allowlist alone is bypassable via DNS, so
    we resolve and reject loopback / private / link-local / reserved / multicast
    (blocks localhost, 127.*, 10/8, 172.16/12, 192.168/16, 169.254/16 metadata,
    ::1, fc00::/7, fe80::/10)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for *_, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop so a public URL can't 302 into a private IP
    (e.g. the cloud metadata endpoint)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or not _host_is_public(parsed.hostname):
            raise urllib.error.HTTPError(newurl, code, "blocked redirect host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_article(raw_url: str) -> tuple[int, str, bytes]:
    """Fetch a source article for the split-view pane. Returns (status, content_type,
    body). HTML is rewritten iframe-ready: own scripts stripped, <base> + our
    highlighter injected, frame-blocking headers dropped (we send our own)."""
    u = urllib.parse.urlparse(raw_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return 400, "text/plain; charset=utf-8", b"http(s) url required"
    if not _host_is_public(u.hostname):
        return 403, "text/plain; charset=utf-8", b"blocked host"

    opener = urllib.request.build_opener(_SafeRedirect())
    req = urllib.request.Request(raw_url, headers={
        "User-Agent": "Mozilla/5.0 (quiz-app article reader)",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with opener.open(req, timeout=ARTICLE_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "text/html")
            data = resp.read(ARTICLE_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 502, "text/html; charset=utf-8", (
            b"<!doctype html><meta charset=utf-8>"
            b"<p style='font:15px system-ui;padding:2rem;color:#555'>"
            b"Could not load the source article.</p>")
    if len(data) > ARTICLE_MAX_BYTES:
        data = data[:ARTICLE_MAX_BYTES]

    # Non-HTML (PDF, etc.): stream through untouched with upstream headers dropped.
    if "html" not in ctype.lower():
        return 200, ctype, data

    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    else:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', data[:4096], re.I)
        if m:
            charset = m.group(1).decode("ascii", "replace")
    try:
        html = data.decode(charset, "replace")
    except LookupError:
        html = data.decode("utf-8", "replace")

    # Neutralize the page's own JS (frame is same-origin to the app) and inject ours.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
    origin = f"{u.scheme}://{u.netloc}/"
    inject = f'<base href="{origin}">' + ARTICLE_HIGHLIGHTER_JS
    if re.search(r"<head[^>]*>", html, re.I):
        html = re.sub(r"(<head[^>]*>)", lambda mo: mo.group(1) + inject, html,
                      count=1, flags=re.I)
    else:
        html = inject + html
    return 200, "text/html; charset=utf-8", html.encode("utf-8")


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


def parse_grade_json(content: str) -> dict:
    """Extract the grader's JSON object, tolerating models that don't honor the
    structured-output `format` param and instead wrap JSON in ```json fences or
    surround it with prose (e.g. the MLX gemma builds). Slices from the first
    '{' to the last '}' so fences and chatter are ignored."""
    s = (content or "").strip()
    if not s:
        raise ValueError("model returned an empty response")
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j <= i:
            raise ValueError("no JSON object found in model response")
        return json.loads(s[i:j + 1])


def grade_answer(question: str, accepted: list, explanation: str,
                 user_answer: str, model: str) -> dict:
    payload = {
        "model": model or AI_DEFAULT_MODEL,
        "stream": False,
        "format": GRADE_SCHEMA,
        "options": {"temperature": 0, "num_predict": 320},
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
        result = parse_grade_json(data["message"]["content"])
        if result.get("verdict") not in ("correct", "partially_correct", "incorrect"):
            raise ValueError("bad verdict")
        return {"ok": True, "verdict": result["verdict"],
                "feedback": result.get("feedback", ""),
                "model": payload["model"], "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        # Ollama IS reachable but rejected the request (e.g. 404 model not found).
        # HTTPError subclasses URLError, so it must be handled before the
        # "not reachable" branch or a missing model gets mislabeled as a dead server.
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        if e.code == 404 or "not found" in detail.lower():
            return {"ok": False, "message": f"Model '{payload['model']}' isn't installed in "
                                            f"Ollama — pull it (`ollama pull {payload['model']}`) "
                                            f"or pick another model in Settings."}
        return {"ok": False, "message": f"Ollama rejected the request (HTTP {e.code})"
                                        f"{': ' + detail if detail else ''}."}
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"ok": False, "message": "Ollama is not reachable on 127.0.0.1:11434 — "
                                        "start it with `ollama serve` (or open the Ollama app)."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Grading failed on {payload['model']}: {e}"}


# ---------------------------------------------------------------- Ollama chat / tutor

CHAT_SYSTEM = (
    "You are a concise, sharp study tutor embedded in a quiz app. You are given "
    "the FULL quiz as context and the ONE question the user is currently looking "
    "at. Help the user understand this question and its topic: explain the "
    "reasoning, why the correct answer is correct and the distractors are wrong, "
    "give intuition, examples, and answer follow-ups. Be direct and brief — a few "
    "short paragraphs at most, no filler. Use the other questions in the quiz for "
    "context but stay focused on what the user asks.\n\n"
    "You may also EDIT the current question when the user asks you to (e.g. "
    "'make it harder', 'rephrase this', 'turn it into open-ended', 'fix the "
    "wording', 'add a better distractor', 'change the answer'). When and ONLY when "
    "you change the question, append at the very end of your reply a fenced code "
    "block tagged `question-edit` containing the COMPLETE updated question as a "
    "single JSON object, with ALL fields the app needs:\n"
    "  - multiple-choice: {\"question\": str, \"type\": \"multiple-choice\", "
    "\"options\": [str,...], \"correctAnswer\": int (0-based index into options), "
    "optional \"explanation\": str}\n"
    "  - open-ended: {\"question\": str, \"type\": \"open-ended\", "
    "\"acceptedAnswers\": [str,...], optional \"explanation\": str}\n"
    "Preserve any existing `image`, `audio`, or `sourceUrl` fields unless the user "
    "asks to remove them. Keep `correctAnswer` pointing at the genuinely correct "
    "option. Never emit the `question-edit` block if you did not change anything. "
    "Above the block, briefly tell the user what you changed."
)


_EDIT_FENCE = re.compile(r"```(?:question-edit|json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def extract_question_edit(reply: str, current: dict | None):
    """Pull a live question edit out of the tutor's reply. Local models don't
    reliably honor the exact `question-edit` fence tag, so we scan every fenced
    block (and a trailing bare JSON object as a fallback), keeping the first one
    that parses AND validates as a real question object. Returns (cleaned_reply,
    edit_or_None) with the matched block stripped from the text."""
    candidates = list(_EDIT_FENCE.finditer(reply))
    spans = [(m.start(), m.end(), m.group(1)) for m in candidates]
    if not spans:  # fallback: a bare {...} object at the very end of the reply
        tail = reply.rstrip()
        j = tail.rfind("}")
        i = tail.rfind("{", 0, j) if j != -1 else -1
        # walk back to the outermost opening brace
        if i != -1 and j > i:
            depth, k = 0, j
            start = None
            for pos in range(j, -1, -1):
                c = tail[pos]
                if c == "}":
                    depth += 1
                elif c == "{":
                    depth -= 1
                    if depth == 0:
                        start = pos
                        break
            if start is not None:
                spans = [(start, len(tail), tail[start:len(tail)])]

    for start, end, blob in spans:
        try:
            obj = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or _valid_question(obj) is not None:
            continue
        # Don't treat an unchanged echo as an edit.
        if current and obj.get("question") == current.get("question") \
                and obj.get("type") == current.get("type") \
                and obj.get("options") == current.get("options") \
                and obj.get("acceptedAnswers") == current.get("acceptedAnswers"):
            cleaned = (reply[:start] + reply[end:]).strip()
            return cleaned, None
        cleaned = (reply[:start] + reply[end:]).strip()
        return cleaned, obj
    return reply.strip(), None


def chat_reply(quiz: list, index: int, messages: list, model: str) -> dict:
    """Free-form tutor chat grounded in the whole quiz + the current question.
    Returns {ok, reply, model, ms}. The reply may contain a ```question-edit
    fenced block that the client parses to mutate the live question."""
    try:
        current = quiz[index] if 0 <= index < len(quiz) else None
    except (TypeError, IndexError):
        current = None
    context = {
        "current_question_index": index,
        "current_question": current,
        "full_quiz": quiz,
    }
    convo = [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "system", "content": "QUIZ CONTEXT (JSON):\n"
            + json.dumps(context, ensure_ascii=False)},
    ]
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            convo.append({"role": role, "content": content})
    if not any(m["role"] == "user" for m in convo):
        return {"ok": False, "message": "no user message"}

    payload = {
        "model": model or AI_DEFAULT_MODEL,
        "stream": False,
        "options": {"temperature": 0.35, "num_predict": 900},
        "keep_alive": AI_KEEP_ALIVE,
        "think": False,
        "messages": convo,
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
        reply = (data.get("message", {}).get("content") or "").strip()
        if not reply:
            return {"ok": False, "message": "model returned an empty response"}
        cleaned, edit = extract_question_edit(reply, current)
        return {"ok": True, "reply": cleaned, "edit": edit, "model": payload["model"],
                "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        if e.code == 404 or "not found" in detail.lower():
            return {"ok": False, "message": f"Model '{payload['model']}' isn't installed "
                                            f"in Ollama — pull it or pick another in Settings."}
        return {"ok": False, "message": f"Ollama rejected the request (HTTP {e.code})"
                                        f"{': ' + detail if detail else ''}."}
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"ok": False, "message": "Ollama is not reachable on 127.0.0.1:11434 — "
                                        "start it with `ollama serve` (or open the Ollama app)."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Chat failed on {payload['model']}: {e}"}


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


def _valid_question(q: dict) -> str | None:
    """Return an error string if the question object is malformed, else None."""
    if not isinstance(q, dict):
        return "question must be an object"
    if not (q.get("question") or "").strip():
        return "question text is required"
    if q.get("type") == "multiple-choice":
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            return "multiple-choice needs an options array of at least 2"
        ca = q.get("correctAnswer")
        if not isinstance(ca, int) or not (0 <= ca < len(opts)):
            return "correctAnswer must be a valid 0-based index into options"
    elif q.get("type") == "open-ended":
        acc = q.get("acceptedAnswers")
        if not isinstance(acc, list) or not acc:
            return "open-ended needs a non-empty acceptedAnswers array"
    else:
        return "type must be 'multiple-choice' or 'open-ended'"
    return None


def update_question(name: str, index: int, old_question_text: str,
                    new_question: dict) -> dict:
    """Overwrite a single question in the on-disk quiz JSON (used by the AI tutor's
    live-edit feature). Backs up to .bak, mirrors delete_question's index-drift
    recovery, and validates the incoming question before writing."""
    err = _valid_question(new_question)
    if err:
        return {"ok": False, "message": f"Invalid edited question: {err}"}
    path = resolve_quiz_path(name)
    if path is None:
        return {"ok": False, "message": f"Quiz file '{name}' not found on disk."}
    quiz = json.loads(path.read_text())
    if not isinstance(quiz, list):
        return {"ok": False, "message": "Quiz file is not a JSON array."}

    if not (0 <= index < len(quiz) and quiz[index].get("question") == old_question_text):
        matches = [i for i, q in enumerate(quiz) if q.get("question") == old_question_text]
        if len(matches) != 1:
            return {"ok": False, "message": "Question not found in the quiz file "
                                            "(it may have been edited elsewhere)."}
        index = matches[0]

    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    quiz[index] = new_question
    path.write_text(json.dumps(quiz, indent=2, ensure_ascii=False) + "\n")
    return {"ok": True, "message": f"Updated question {index + 1} in {path.name}. "
                                   f"Backup: {path.name}.bak"}


def delete_quiz(name: str) -> dict:
    """Remove a whole quiz file from the home-screen list by moving it (and any
    sidecar .bak) into ROOT/.trash — reversible, so the on-disk list can be
    pruned like a log without hard-deleting anything."""
    path = resolve_quiz_path(name)
    if path is None:
        return {"ok": False, "message": f"Quiz file '{name}' not found on disk."}
    trash = ROOT / ".trash"
    trash.mkdir(exist_ok=True)
    dest = trash / path.name
    if dest.exists():  # keep an older trashed copy of the same name
        dest = trash / f"{path.stem}-{int(path.stat().st_mtime)}{path.suffix}"
    shutil.move(str(path), str(dest))
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        try:
            shutil.move(str(bak), str(trash / bak.name))
        except OSError:
            pass
    REGISTERED.pop(path.name, None)
    remaining = sorted(p.name for p in ROOT.glob("*.json"))
    return {"ok": True, "message": f"Moved {path.name} to .trash/ ({len(remaining)} left).",
            "remaining": remaining}


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

        if route == "/api/article":
            status, ctype, body = fetch_article(params.get("url", [""])[0])
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

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

        if route == "/api/ai/chat":
            quiz = body.get("quiz")
            if not isinstance(quiz, list):
                return self._json({"ok": False, "message": "quiz array required"}, 400)
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                return self._json({"ok": False, "message": "messages array required"}, 400)
            index = body.get("currentIndex")
            index = index if isinstance(index, int) else -1
            return self._json(chat_reply(quiz, index, messages,
                                         (body.get("model") or "").strip()))

        if route == "/api/quiz/update-question":
            name = body.get("file") or ""
            index = body.get("index")
            old_q = body.get("oldQuestion") or ""
            new_q = body.get("question")
            if not name or index is None or not old_q or not isinstance(new_q, dict):
                return self._json({"ok": False, "message": "file, index, oldQuestion "
                                                           "and question required"}, 400)
            result = update_question(name, int(index), old_q, new_q)
            return self._json(result, 200 if result["ok"] else 409)

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

        if route == "/api/quiz/delete":
            name = body.get("file") or ""
            if not name:
                return self._json({"ok": False, "message": "file required"}, 400)
            result = delete_quiz(name)
            return self._json(result, 200 if result["ok"] else 404)

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
