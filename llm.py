"""Перевод и подсказки ответов через локальную LLM (Ollama)."""
from __future__ import annotations

import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# Экономия видеопамяти: один запрос за раз, сжатый кэш контекста, одна модель в памяти.
# Нам этого достаточно — фразы переводятся по очереди.
ECONOMY_ENV = {
    "OLLAMA_NUM_PARALLEL": "1",
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
    "OLLAMA_MAX_LOADED_MODELS": "1",
}


class Ollama:
    def __init__(self, url: str, model: str, timeout: float = 180, status=None):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.status = status or (lambda s: None)
        self._start_lock = threading.Lock()
        self._last_start = 0.0
        self._pull_lock = threading.Lock()
        self.problem = ""  # последняя понятная причина недоступности
        self.warning = ""  # например: модель работает на процессоре
        # Фразы короткие: 2048 токенов контекста хватает с запасом, а видеопамяти уходит в разы меньше,
        # чем при настройках Ollama по умолчанию
        self.num_ctx = 2048

    # ------------------------------------------------------------ автозапуск Ollama
    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/api/version", timeout=2):
                return True
        except Exception:
            return False

    @staticmethod
    def find_exe() -> str | None:
        cands = [shutil.which("ollama")]
        la = os.environ.get("LOCALAPPDATA", "")
        drive = os.path.splitdrive(HERE)[0]
        cands += [
            os.path.join(la, "Programs", "Ollama", "ollama.exe"),
            os.path.join(drive + os.sep, "Ollama", "ollama.exe"),
            os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe"),
        ]
        return next((c for c in cands if c and os.path.isfile(c)), None)

    def ensure_running(self) -> bool:
        """Если Ollama не отвечает — запускаем `ollama serve` в фоне и ждём до 20 с."""
        if self.alive():
            return True
        with self._start_lock:
            if self.alive():
                return True
            if time.monotonic() - self._last_start < 25:  # не перезапускаем по кругу
                return False
            self._last_start = time.monotonic()
            exe = self.find_exe()
            if not exe:
                self.problem = "Ollama не установлена — скачайте с ollama.com, установите и перезапустите оверлей"
                self.status(self.problem)
                return False
            env = dict(os.environ)
            for k, v in ECONOMY_ENV.items():
                env.setdefault(k, v)
            if not env.get("OLLAMA_MODELS"):
                local = os.path.join(os.path.splitdrive(HERE)[0] + os.sep, "Ollama", "models")
                if os.path.isdir(local):
                    env["OLLAMA_MODELS"] = local
            self.status("Запускаю Ollama…")
            flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            try:
                subprocess.Popen([exe, "serve"], env=env, creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except Exception as e:
                self.problem = f"Не удалось запустить Ollama: {e}"
                self.status(self.problem)
                return False
            for _ in range(40):
                time.sleep(0.5)
                if self.alive():
                    self.problem = ""
                    self.status("Ollama запущена")
                    return True
            self.problem = "Ollama не отвечает — запустите её вручную из меню Пуск"
            self.status(self.problem)
            return False

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def unload(self, model: str):
        """Выгрузить модель из видеопамяти."""
        try:
            self._post("/api/generate", {"model": model, "keep_alive": 0})
        except Exception:
            pass

    def restart_economy(self) -> bool:
        """Перезапустить Ollama с экономными настройками (если её запускали без них, например из трея)."""
        if sys.platform == "win32":
            for im in ("ollama app.exe", "ollama.exe"):
                subprocess.run(["taskkill", "/f", "/im", im], capture_output=True, creationflags=0x08000000)
        time.sleep(1.5)
        self._last_start = 0.0
        return self.ensure_running()

    def has_model(self) -> bool:
        with urllib.request.urlopen(self.url + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = {m.get("name", "") for m in tags.get("models", [])}
        want = self.model if ":" in self.model else self.model + ":latest"
        return want in names

    def pull(self) -> bool:
        """Скачивает модель через Ollama, показывая прогресс в строке статуса."""
        with self._pull_lock:
            try:
                if self.has_model():
                    return True
            except Exception:
                pass
            self.status(f"Скачиваю модель перевода {self.model} (только в первый раз)…")
            req = urllib.request.Request(
                self.url + "/api/pull",
                data=json.dumps({"model": self.model, "stream": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            last = 0.0
            try:
                with urllib.request.urlopen(req, timeout=None) as r:
                    for line in r:
                        if not line.strip():
                            continue
                        msg = json.loads(line.decode("utf-8"))
                        if msg.get("error"):
                            raise RuntimeError(msg["error"])
                        total, done = msg.get("total") or 0, msg.get("completed") or 0
                        if total and time.monotonic() - last > 1:
                            last = time.monotonic()
                            self.status(f"Скачиваю модель {self.model}: {done * 100 // total}% "
                                        f"({done / 1e9:.1f} из {total / 1e9:.1f} ГБ)")
                        if msg.get("status") == "success":
                            self.status(f"Модель {self.model} скачана")
                            return True
            except Exception as e:
                self.problem = f"Не удалось скачать модель {self.model}: {e}. Выполните вручную: ollama pull {self.model}"
                self.status(self.problem)
                return False
            return self.has_model()

    def check(self) -> str | None:
        """None — всё хорошо, иначе текст проблемы. Недостающую модель скачивает сама."""
        if not self.ensure_running():
            return self.problem or "Ollama не запущена"
        try:
            if self.has_model():
                return None
        except Exception as e:
            return f"Ollama не отвечает: {e}"
        return None if self.pull() else self.problem

    def _explain(self, e: Exception) -> RuntimeError:
        """Понятный текст ошибки вместо «connection refused» / «HTTP 404»."""
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            if e.code == 404:
                return RuntimeError(f"модель {self.model} не скачана — выполните: ollama pull {self.model}")
            return RuntimeError(f"Ollama ответила ошибкой {e.code}: {detail or e.reason}")
        return RuntimeError(self.problem or "Ollama не запущена")

    def _with_recovery(self, fn):
        """Выполняет запрос; если Ollama не запущена — запускает, если нет модели — скачивает, и повторяет."""
        try:
            return fn()
        except TimeoutError as e:
            raise RuntimeError("модель перевода слишком долго отвечает — " + self.slow_hint()) from e
        except urllib.error.HTTPError as e:
            if e.code == 404 and self.pull():
                return fn()
            raise self._explain(e) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise RuntimeError("модель перевода слишком долго отвечает — " + self.slow_hint()) from e
            if not self.ensure_running():
                raise self._explain(e) from e
            return fn()

    def placement(self) -> tuple[int, int] | None:
        """(весь размер, сколько в видеопамяти) загруженной модели — по /api/ps."""
        try:
            with urllib.request.urlopen(self.url + "/api/ps", timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None
        base = self.model.split(":")[0]
        for m in data.get("models", []):
            if (m.get("name") or m.get("model") or "").split(":")[0] == base:
                return int(m.get("size") or 0), int(m.get("size_vram") or 0)
        return None

    def slow_hint(self) -> str:
        p = self.placement()
        if p and p[0] and p[1] == 0:
            return ("она работает на процессоре, без видеокарты. Поставьте в config.toml "
                    "[llm] model = \"gemma3:1b\" — будет быстрее, но перевод проще")
        if p and p[0] and p[1] < p[0]:
            return ("не вся модель поместилась в видеопамять. Закройте игры/браузер с видео "
                    "или поставьте в config.toml [llm] model = \"gemma3:1b\"")
        return "проверьте, что Ollama видит видеокарту (обновите драйвер), или возьмите модель поменьше: gemma3:1b"

    def chat(self, system: str, user: str, json_mode: bool = False, temperature: float = 0.2,
             num_predict: int = 500) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": -1,  # держать модель в памяти — меньше задержка
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_ctx": self.num_ctx, "num_predict": num_predict},
        }
        if json_mode:
            payload["format"] = "json"
        return self._with_recovery(lambda: self._post("/api/chat", payload)["message"]["content"].strip())

    def chat_stream(self, system: str, user: str, on_delta, temperature: float = 0.1, num_predict: int = 200) -> str:
        """Потоковый ответ: on_delta(весь_текст_на_данный_момент) -> True, чтобы прервать генерацию."""
        payload = {
            "model": self.model,
            "stream": True,
            "keep_alive": -1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_ctx": self.num_ctx, "num_predict": num_predict},
        }

        def run() -> str:
            req = urllib.request.Request(
                self.url + "/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            text = ""
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for line in r:  # NDJSON: одна строка — один кусочек ответа
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line.decode("utf-8"))
                    if msg.get("error"):
                        raise RuntimeError(msg["error"])
                    text += msg.get("message", {}).get("content", "")
                    if msg.get("done"):
                        break
                    if on_delta(text):  # прерывание: закрытие соединения останавливает генерацию
                        break
            return text.strip()

        return self._with_recovery(run)


def _clean_partial(s: str) -> str:
    """Очистка незаконченного куска: теги, «Перевод:», открывающая кавычка."""
    s = re.sub(r"</?(utterance|context|phrase)>?", "", s, flags=re.I).lstrip()
    s = re.sub(r"^(translation|перевод|english|russian)\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"^<[^>]*$", "", s)  # недописанный тег ещё не показываем
    s = s.lstrip("\"«'“")
    return s.split("\n")[0] if "\n" in s.strip() else s


def _clean(s: str) -> str:
    s = s.strip()
    s = re.sub(r"</?(utterance|context|phrase)>", "", s, flags=re.I).strip()
    s = re.sub(r"^(translation|перевод|english|russian)\s*:\s*", "", s, flags=re.I)
    s = s.split("\n")[0].strip() if "\n" in s and len(s.split("\n")[0]) > 3 else s
    if len(s) >= 2 and s[0] in "\"«'“" and s[-1] in "\"»'”":
        s = s[1:-1].strip()
    return s


_RULES = (
    " The message may contain <context> with EARLIER lines of the call: NEVER translate or repeat them, "
    "use them only to understand references. Translate ONLY the text inside <utterance>. "
    "If it is a question, translate the question — do not answer it. Output nothing but the translation."
)
SYS_EN_RU = (
    "You are a live interpreter in a voice call. Translate the English utterance into natural, "
    "colloquial Russian. It comes from speech recognition and may have errors or be cut mid-sentence: "
    "infer the intended meaning, keep it short and faithful." + _RULES
)
SYS_RU_EN = (
    "You help a Russian speaker talk in an English voice call. Turn the Russian utterance into "
    "natural, simple spoken English the user can say out loud right now. Match the conversation's tone." + _RULES
)
SYS_SUGGEST = (
    "You assist a Russian speaker ('Me') in an English voice call with 'Them'. {about}"
    "Suggest {n} different replies Me could say next, in natural, simple spoken English (up to ~25 words each). "
    "If Them has just asked a question, EVERY option must answer exactly that question, in different ways: "
    "a short direct answer, a fuller answer with a detail or reason, and a polite way to buy time or ask to clarify. "
    "Otherwise suggest a natural reaction, a follow-up question and a short acknowledgement. "
    "Do not invent specific facts about Me that are not known — keep such parts generic "
    "or put a short placeholder in square brackets, e.g. [date]. "
    'Return JSON: {{"replies":[{{"en":"...","ru":"..."}}]}} where "ru" is the Russian translation of "en".'
)


class LLMWorker(threading.Thread):
    """Последовательная очередь задач к LLM. Переводы приоритетнее подсказок."""

    def __init__(self, client: Ollama, context_lines: int = 3):
        super().__init__(daemon=True)
        self.client = client
        self.context_lines = context_lines
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.seq = itertools.count()
        self.suggest_pending = False
        self._sugg_latest = None
        self.lock = threading.Lock()

    def run(self):
        while True:
            _, _, fn = self.q.get()
            try:
                fn()
            except Exception as e:
                print("LLM error:", e)

    def _ctx(self, context: list[str]) -> str:
        return "\n".join(context[-8:]) if context else "(start of conversation)"

    def translate(self, text: str, direction: str, context: list[str], cb, on_partial=None):
        """cb(готовый_перевод); on_partial(текст_по_мере_генерации) — для потокового показа."""
        system = SYS_EN_RU if direction == "en-ru" else SYS_RU_EN
        ctx_lines = self.context_lines
        limit = 2.5 * len(text) + 25  # длиннее — модель явно переводит что-то лишнее

        def ask(with_ctx: bool) -> tuple[str, bool]:
            user = ""
            if with_ctx and context and ctx_lines > 0:
                user += "<context>\n" + "\n".join(context[-ctx_lines:]) + "\n</context>\n\n"
            user += f"<utterance>\n{text}\n</utterance>"
            state = {"runaway": False, "last": 0.0}

            def on_delta(raw: str) -> bool:
                shown = _clean_partial(raw)
                if len(shown) > limit:
                    state["runaway"] = True
                    return True
                stripped = raw.strip()
                if "\n" in stripped and len(stripped.split("\n")[0]) > 3:
                    return True  # первая строка готова — дальше модель обычно добавляет лишнее
                now = time.monotonic()
                if on_partial and shown and now - state["last"] > 0.04:
                    state["last"] = now
                    on_partial(shown)
                return False

            out = _clean(self.client.chat_stream(system, user, on_delta, temperature=0.1))
            return out, state["runaway"]

        def job():
            try:
                out, runaway = ask(True)
                # Защита от «перевода не того»: слишком длинный ответ или совпадение с контекстом
                echoes = len(out) > 12 and any(out.lower() in c.lower() for c in context[-ctx_lines:])
                if runaway or echoes or len(out) > limit or not out:
                    out, _ = ask(False)
                cb(out)
            except Exception as e:
                cb(f"[ошибка перевода: {e}]")

        self.q.put((0, next(self.seq), job))

    def suggest(self, context: list[str], n: int, cb, question: str | None = None, about: str = ""):
        """Варианты ответа. Если запрос уже в работе — запомним самый свежий и выполним его следом."""
        with self.lock:
            self._sugg_latest = (context, n, cb, question, about)
            if self.suggest_pending:
                return
            self.suggest_pending = True
        self.q.put((1, next(self.seq), self._suggest_job))

    def _suggest_job(self):
        with self.lock:
            context, n, cb, question, about = self._sugg_latest
            self._sugg_latest = None
        try:
            about_txt = f"About Me: {about.strip()}. " if about and about.strip() else ""
            user = f"Transcript:\n{self._ctx(context)}\n\n"
            if question:
                user += f"Them just asked: \"{question}\"\nSuggest {n} ways for Me to answer this question."
            else:
                user += f"Suggest {n} replies for Me."
            raw = self.client.chat(SYS_SUGGEST.format(n=n, about=about_txt), user, json_mode=True, temperature=0.6)
            data = json.loads(raw)
            items = data.get("replies", []) if isinstance(data, dict) else data
            out = [
                {"en": str(i.get("en", "")).strip(), "ru": str(i.get("ru", "")).strip()}
                for i in items
                if isinstance(i, dict) and i.get("en")
            ][:n]
            cb(out, question or "")
        except Exception as e:
            cb([{"en": f"[ошибка подсказок: {e}]", "ru": ""}], question or "")
        finally:
            with self.lock:
                again = self._sugg_latest is not None  # пока думали, пришёл новый вопрос
                self.suggest_pending = again
            if again:
                self.q.put((1, next(self.seq), self._suggest_job))

    def warmup(self, on_loaded=None):
        """Загружаем модель в память заранее, чтобы первый перевод не ждал.

        on_loaded((размер, в_видеопамяти) | None) -> True, если колбэк сам разобрался (например, сменил модель).
        """
        c = self.client

        def job():
            c.status(f"Загружаю модель перевода {c.model} в память…")
            t0 = time.monotonic()
            old = c.timeout
            c.timeout = 600  # первый раз модель читается с диска — бывает долго
            try:
                c.chat("Reply with OK.", "OK", num_predict=5)
            except Exception as e:
                c.status(f"Модель перевода не загрузилась: {e}")
                return
            finally:
                c.timeout = old
            p = c.placement()
            secs = time.monotonic() - t0
            if on_loaded and on_loaded(p):
                return
            c.warning = ""
            if p and p[0] and p[1] == 0:
                c.warning = ("⚠ Модель перевода работает на процессоре, без видеокарты — будет медленно. "
                             "Для скорости: config.toml → [llm] model = \"gemma3:1b\"")
                c.status(c.warning)
            elif p and p[0] and p[1] < p[0]:
                c.warning = (f"⚠ В видеопамять поместилось {p[1] * 100 // p[0]}% модели перевода — будет медленнее. "
                             "Закройте игры/видео или поставьте config.toml → [llm] model = \"qwen2.5:3b\"")
                c.status(c.warning)
            else:
                c.status(f"Модель перевода готова ({secs:.0f} с, целиком на видеокарте)")

        self.q.put((2, next(self.seq), job))
