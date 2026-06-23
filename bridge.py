import json
import bisect
import hashlib
import threading
import subprocess
import time
import http.client
import atexit
from datetime import date
from pathlib import Path

try:
    import wn as _wn
    _WN_IMPORT_OK = True
except ImportError:
    _WN_IMPORT_OK = False

try:
    import nltk
    from nltk.corpus import cmudict
    _CMU_IMPORT_OK = True
except ImportError:
    _CMU_IMPORT_OK = False

try:
    import pyttsx3
    _TTS_OK = True
except ImportError:
    _TTS_OK = False

from orbit_embed import OrbitBridge

# CMU phoneme → IPA character
_CMU_IPA = {
    "AA": "ɑ",  "AE": "æ",  "AH": "ə",  "AO": "ɔ",  "AW": "aʊ",
    "AY": "aɪ", "EH": "ɛ",  "ER": "ɜr", "EY": "eɪ", "IH": "ɪ",
    "IY": "iː", "OW": "oʊ", "OY": "ɔɪ", "UH": "ʊ",  "UW": "uː",
    "B":  "b",  "CH": "tʃ", "D":  "d",  "DH": "ð",  "F":  "f",
    "G":  "g",  "HH": "h",  "JH": "dʒ", "K":  "k",  "L":  "l",
    "M":  "m",  "N":  "n",  "NG": "ŋ",  "P":  "p",  "R":  "r",
    "S":  "s",  "SH": "ʃ",  "T":  "t",  "TH": "θ",  "V":  "v",
    "W":  "w",  "Y":  "j",  "Z":  "z",  "ZH": "ʒ",
}

_POS = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}
_POS_RANK = {"noun": 0, "verb": 1, "adjective": 2, "adverb": 3}

_SERVER_HOST = "127.0.0.1"
_SERVER_PORT = 8189

_LLM_SYSTEM = (
    "You are Lingua, an expert lexicographer and linguist. "
    "Help users deeply understand words — their meanings, nuances, etymology, and usage. "
    "Be precise, insightful, and engaging. "
    "Write in flowing prose (2–4 short paragraphs max). No bullet lists."
)


class LinguaBridge(OrbitBridge):

    def __init__(self, data_root: Path, model_path: Path):
        super().__init__()
        self._data = Path(data_root)
        self._data.mkdir(parents=True, exist_ok=True)
        self._favs_path = self._data / "favorites.json"
        self._hist_path = self._data / "history.json"

        self._cmu: dict = {}
        self._words: list = []
        self._wn_ready = False
        self._server_proc = None
        self._llm_ready = False
        self._llm_loading = False

        if _WN_IMPORT_OK:
            threading.Thread(target=self._init_wn, daemon=True).start()

        if _CMU_IMPORT_OK:
            nltk_path = str(self._data / "nltk_data")
            if nltk_path not in nltk.data.path:
                nltk.data.path.insert(0, nltk_path)
            try:
                self._cmu = dict(cmudict.entries())
            except Exception as exc:
                print(f"[bridge] CMU dict load error: {exc}")

        model_path = Path(model_path)
        if model_path.exists():
            self._model_path = model_path
            threading.Thread(target=self._start_server, daemon=True).start()

    # ── llama-server subprocess ────────────────────────────────────────────

    def _start_server(self):
        self._llm_loading = True
        base = Path(__file__).parent

        # Prefer the CUDA build; fall back to the CPU build
        server = base / "llama.cpp" / "cuda" / "llama-server.exe"
        if not server.exists():
            server = base / "llama.cpp" / "llama-server.exe"
        if not server.exists():
            print("[bridge] llama-server.exe not found")
            self._llm_loading = False
            self.emit_event("llmReady", {"ready": False})
            return

        cmd = [
            str(server),
            "-m",  str(self._model_path),
            "--host", _SERVER_HOST,
            "--port", str(_SERVER_PORT),
            "-ngl", "99",          # offload all layers to GPU
            "--ctx-size", "4096",
            "--log-disable",
        ]
        print(f"[bridge] starting llama-server on port {_SERVER_PORT} …")
        try:
            self._server_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            atexit.register(self._stop_server)
        except Exception as exc:
            print(f"[bridge] failed to start server: {exc}")
            self._llm_loading = False
            self.emit_event("llmReady", {"ready": False})
            return

        # Poll /health until the server is ready (up to 120 s)
        for _ in range(240):
            time.sleep(0.5)
            try:
                conn = http.client.HTTPConnection(_SERVER_HOST, _SERVER_PORT, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                if resp.status == 200:
                    self._llm_ready = True
                    break
                conn.close()
            except Exception:
                pass

        self._llm_loading = False
        print(f"[bridge] llama-server ready: {self._llm_ready}")
        self.emit_event("llmReady", {"ready": self._llm_ready})

    def _stop_server(self):
        if self._server_proc and self._server_proc.poll() is None:
            self._server_proc.terminate()
            try:
                self._server_proc.wait(timeout=3)
            except Exception:
                self._server_proc.kill()

    # ── WordNet init (background) ─────────────────────────────────────────

    def _init_wn(self):
        try:
            _wn.download('oewn:2024')
            self._words = sorted(set(_wn.lemmas(lang='en')))
            if not self._words:
                raise RuntimeError("wordnet returned no words")
            self._wn_ready = True
            print(f"[bridge] OEWN loaded ({len(self._words):,} words)")
            self.emit_event('wnReady', {'ready': True})
        except Exception as exc:
            print(f"[bridge] WordNet load error: {exc}")
            self.emit_event('wnReady', {'ready': False})

    # ── Dispatch ──────────────────────────────────────────────────────────

    def handle_invoke(self, method: str, args: list):
        match method:
            case "searchWords":    return self._search(args[0] if args else "")
            case "getWord":        return self._get_word(args[0] if args else "")
            case "getWordOfDay":   return self._wotd()
            case "speakWord":      return self._speak(args[0] if args else "")
            case "getFavorites":   return self._get_favs()
            case "toggleFavorite": return self._toggle_fav(args[0] if args else "")
            case "getHistory":     return self._get_hist()
            case "addToHistory":   return self._add_hist(args[0] if args else "")
            case "getLlmStatus":   return {"ready": self._llm_ready, "loading": self._llm_loading}
            case "askLlm":
                prompt  = args[0] if len(args) > 0 else ""
                context = args[1] if len(args) > 1 else {}
                return self._ask_llm(prompt, context)
        return super().handle_invoke(method, args)

    # ── Dictionary ────────────────────────────────────────────────────────

    def _search(self, query: str) -> dict:
        q = query.strip().lower()
        if not q or not self._wn_ready:
            return {"results": [], "query": query}

        results, seen = [], set()

        idx = bisect.bisect_left(self._words, q)
        while idx < len(self._words) and self._words[idx].startswith(q):
            lemma = self._words[idx]
            idx += 1
            if lemma in seen:
                continue
            seen.add(lemma)
            ss = _wn.synsets(lemma, lang='en')
            if ss:
                results.append({
                    "word":     lemma.replace("_", " "),
                    "pos":      _POS.get(ss[0].pos, ss[0].pos),
                    "shortDef": ss[0].definition(),
                })
            if len(results) >= 25:
                break

        if not results:
            matching = _wn.words(q, lang='en')
            if matching:
                base = matching[0].lemma()
                if base not in seen:
                    ss = matching[0].synsets()
                    if ss:
                        results.append({
                            "word":     base.replace("_", " "),
                            "pos":      _POS.get(ss[0].pos, ss[0].pos),
                            "shortDef": ss[0].definition(),
                        })

        return {"results": results, "query": query}

    def _get_word(self, word: str) -> dict:
        if not word or not self._wn_ready:
            return {"found": False, "word": word}

        key = word.strip().lower()
        synsets = _wn.synsets(key, lang='en')
        if not synsets:
            matching = _wn.words(key, lang='en')
            if matching:
                key = matching[0].lemma()
                synsets = _wn.synsets(key, lang='en')
        if not synsets:
            return {"found": False, "word": word}

        by_pos: dict[str, dict] = {}
        for ss in synsets:
            pos = _POS.get(ss.pos, ss.pos)
            if pos not in by_pos:
                by_pos[pos] = {
                    "pos": pos, "definitions": [],
                    "synonyms": [], "antonyms": [],
                    "hypernyms": [], "hyponyms": [],
                    "_seen": {"syn": set(), "ant": set(), "hyper": set(), "hypo": set()},
                }
            e = by_pos[pos]
            seen = e["_seen"]
            word_lower = key.lower()
            filtered_examples = [ex for ex in ss.examples() if word_lower in ex.lower()]
            e["definitions"].append({"text": ss.definition(), "examples": filtered_examples})

            # Synonyms: co-lemmas in the same synset
            for nm in ss.lemmas():
                nm_clean = nm.replace("_", " ")
                if nm.lower() != word_lower and nm not in seen["syn"]:
                    e["synonyms"].append(nm_clean)
                    seen["syn"].add(nm)

            # Antonyms: accessed through senses in the wn library
            for sense in ss.senses():
                for ant_sense in sense.get_related('antonym'):
                    ant_nm = ant_sense.word().lemma().replace("_", " ")
                    if ant_nm not in seen["ant"]:
                        e["antonyms"].append(ant_nm)
                        seen["ant"].add(ant_nm)

            for h in ss.hypernyms():
                for nm in h.lemmas():
                    nm_clean = nm.replace("_", " ")
                    if nm_clean not in seen["hyper"]:
                        e["hypernyms"].append(nm_clean)
                        seen["hyper"].add(nm_clean)
            for h in ss.hyponyms():
                for nm in h.lemmas():
                    nm_clean = nm.replace("_", " ")
                    if nm_clean not in seen["hypo"]:
                        e["hyponyms"].append(nm_clean)
                        seen["hypo"].add(nm_clean)

        entries = sorted(
            [{
                "pos":         d["pos"],
                "definitions": d["definitions"],
                "synonyms":    d["synonyms"][:15],
                "antonyms":    d["antonyms"][:10],
                "hypernyms":   d["hypernyms"][:8],
                "hyponyms":    d["hyponyms"][:8],
            } for d in by_pos.values()],
            key=lambda e: _POS_RANK.get(e["pos"], 9),
        )

        return {
            "found":    True,
            "word":     key.replace("_", " "),
            "phonetic": self._phonetic(key.replace("_", " ")),
            "entries":  entries,
        }

    def _phonetic(self, word: str) -> str:
        phones = self._cmu.get(word.lower())
        if not phones:
            return ""
        ipa = []
        for ph in phones:
            digit  = ph[-1] if ph[-1].isdigit() else "0"
            base   = ph.rstrip("012")
            prefix = "ˈ" if digit == "1" else "ˌ" if digit == "2" else ""
            ipa.append(prefix + _CMU_IPA.get(base, base.lower()))
        return "/" + "".join(ipa) + "/"

    def _wotd(self) -> dict:
        seed = int(hashlib.md5(date.today().isoformat().encode()).hexdigest(), 16)
        pool = [w for w in self._words if 5 <= len(w) <= 11 and "_" not in w and " " not in w]
        if not pool:
            return {"found": False}
        return self._get_word(pool[seed % len(pool)])

    # ── TTS ───────────────────────────────────────────────────────────────

    def _speak(self, word: str) -> dict:
        if not _TTS_OK:
            return {"ok": False, "error": "pyttsx3 not installed"}

        def _run():
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", 140)
                engine.say(word)
                engine.runAndWait()
                engine.stop()
            except Exception as exc:
                print(f"[bridge] TTS error: {exc}")

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True}

    # ── Persistence ───────────────────────────────────────────────────────

    def _read_json(self, path: Path, default):
        try:
            if path.exists():
                return json.loads(path.read_text("utf-8"))
        except Exception:
            pass
        return default

    def _write_json(self, path: Path, data) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _get_favs(self) -> dict:
        return {"favorites": self._read_json(self._favs_path, [])}

    def _toggle_fav(self, word: str) -> dict:
        favs = self._read_json(self._favs_path, [])
        if word in favs:
            favs.remove(word)
            action = "removed"
        else:
            favs.insert(0, word)
            action = "added"
        self._write_json(self._favs_path, favs)
        return {"favorites": favs, "action": action, "word": word}

    def _get_hist(self) -> dict:
        return {"history": self._read_json(self._hist_path, [])}

    def _add_hist(self, word: str) -> dict:
        hist = self._read_json(self._hist_path, [])
        if word in hist:
            hist.remove(word)
        hist.insert(0, word)
        hist = hist[:50]
        self._write_json(self._hist_path, hist)
        return {"history": hist}

    # ── LLM ───────────────────────────────────────────────────────────────

    def _ask_llm(self, prompt: str, context: dict) -> dict:
        if not self._llm_ready:
            return {"started": False, "error": "LLM not ready"}
        word = context.get("word", "")
        user_content = (
            f'/no_think The user is currently viewing the word "{word}". '
            f"Assume their question is about that word unless they specify otherwise.\n\n{prompt}"
            if word else f"/no_think {prompt}"
        )
        messages = [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user",   "content": user_content},
        ]
        threading.Thread(target=self._stream, args=(messages,), daemon=True).start()
        return {"started": True}

    def _stream(self, messages: list) -> None:
        try:
            payload = json.dumps({
                "model": "local",
                "messages": messages,
                "stream": True,
                "max_tokens": 2048,
                "temperature": 0.65,
                "chat_template_kwargs": {"enable_thinking": False},
            }).encode()

            conn = http.client.HTTPConnection(_SERVER_HOST, _SERVER_PORT, timeout=120)
            conn.request("POST", "/v1/chat/completions", payload, {
                "Content-Type": "application/json",
            })
            resp = conn.getresponse()
            print(f"[bridge] chat status: {resp.status}")

            if resp.status != 200:
                body = resp.read().decode("utf-8", errors="replace")
                print(f"[bridge] error body: {body[:500]}")
                self.emit_event("llmToken", {"token": f"[Server error {resp.status}]", "done": False})
                return

            token_count = 0
            is_thinking = False

            while True:
                raw_line = resp.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"]
                    reasoning = delta.get("reasoning_content") or ""
                    content   = delta.get("content") or ""
                except Exception as e:
                    print(f"[bridge] parse error: {e}")
                    continue

                if reasoning and not is_thinking:
                    is_thinking = True
                    self.emit_event("llmThinking", {"active": True})

                if content:
                    if is_thinking:
                        is_thinking = False
                        self.emit_event("llmThinking", {"active": False})
                    token_count += 1
                    if token_count == 1:
                        print(f"[bridge] first token: {repr(content[:60])}")
                    self.emit_event("llmToken", {"token": content, "done": False})

            print(f"[bridge] stream done. tokens={token_count}")

        except Exception as exc:
            print(f"[bridge] stream error: {exc}")
            self.emit_event("llmToken", {"token": f"\n[Error: {exc}]", "done": False})
        finally:
            self.emit_event("llmThinking", {"active": False})
            self.emit_event("llmToken", {"token": "", "done": True})
