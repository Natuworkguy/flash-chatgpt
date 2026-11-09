"""ChatGPT and API-key models for Flash through codex app-server.

Uses Codex's current authentication. API-key requests use API billing.
ChatGPT logins retain the included-usage and plan checks.
Flash supplies the tools and handles their execution and approval.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess  # nosec B404 -- codex app-server, the program this speaks to
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
HERE = Path(os.environ.get("FLASH_EXTENSION_DIR") or Path(__file__).parent)
CACHE = HERE / ".cache"
MODELS_TTL = 600

# How long Codex has to answer one request of ours (starting up, listing
# models, starting a thread), and to go quiet before a turn is given up.
ANSWER_SECONDS = 60
QUIET_SECONDS = 540
# Once the model calls a tool, how long to wait for the others it calls
# with it before handing them all to Flash.
MORE_CALLS_SECONDS = 0.3

EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# Plans billed by what is used, not given usage with the plan.
BILLED_BY_USE = ("self_serve_business_usage_based",
                 "enterprise_cbp_usage_based")

NOTE = "[A note from Flash]"
TOOL_ENVIRONMENT = (
    "You are running inside Flash. The read-only sandbox describes Codex's "
    "built-in execution environment, not the Flash tools supplied to this "
    "thread. Flash executes those tools and applies its own permissions and "
    "approval checks. Use the supplied Flash tools for file edits, writes, "
    "and commands when the task calls for them. Do not infer that Flash's "
    "tools are read-only from Codex's sandbox setting. If a Flash tool "
    "actually denies an operation, respect that denial and report it."
)
CONTINUE = (f"{NOTE} The results of the tools you called are above. Carry "
            "on from them.")

NO_CODEX = (
    "Codex is not installed. Install it (npm install -g @openai/codex, or "
    "brew install codex), run `codex login` and sign in with your ChatGPT "
    "account, then try again. If it is installed somewhere Flash cannot "
    "see, set its path in this extension's settings (Codex program)."
)
NOT_SIGNED_IN = (
    "Codex is not signed in. Run `codex login` to sign in with ChatGPT "
    "or configure an API key."
)
BILLED_PLAN = (
    "Your ChatGPT workspace is billed by use, not given usage with the "
    "plan, so these models are not used from it."
)


def say(answer: dict) -> None:
    print(json.dumps(answer), flush=True)


class CodexError(Exception):
    """Something Codex refused or could not do, said for the user."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# Settings --------------------------------------------------------------------


def setting(name: str) -> str:
    return os.environ.get(f"FLASH_SETTING_{name.upper()}", "").strip()


def codex_program() -> str:
    """The codex program: the setting's, else the one on PATH."""

    given = setting("codex")
    if given:
        if Path(given).is_file():
            return given
        found = shutil.which(given)
        if found:
            return found
        raise CodexError(f"There is no codex program at {given}. "
                         + NO_CODEX, 404)
    found = shutil.which("codex")
    if not found:
        raise CodexError(NO_CODEX, 404)
    return found


# Talking to codex app-server -------------------------------------------------


class AppServer:
    """`codex app-server`, spoken to over its stdin and stdout: JSON-RPC,
    a message a line, without the "jsonrpc" field."""

    def __init__(self, argv: list[str] | None = None) -> None:
        argv = argv or [codex_program(), "app-server"]
        try:
            self.process = subprocess.Popen(  # nosec B603 -- codex itself
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
            )
        except OSError as exc:
            raise CodexError(f"Could not start Codex: {exc}", 500) from None
        self.lines: queue.Queue = queue.Queue()
        self.errors: list[str] = []
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                found = json.loads(line)
            except ValueError:
                continue
            if isinstance(found, dict):
                self.lines.put(found)
        self.lines.put(None)

    def _read_errors(self) -> None:
        for line in self.process.stderr:
            self.errors.append(line)
            del self.errors[:-40]

    def send(self, message: dict) -> None:
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except OSError:
            raise CodexError(self._gone()) from None

    def _gone(self) -> str:
        said = "".join(self.errors).strip().splitlines()
        return "Codex stopped" + (f": {said[-1]}" if said else ".")

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"method": method, **({"params": params}
                                        if params is not None else {})})

    def respond(self, request_id: Any, result: dict) -> None:
        self.send({"id": request_id, "result": result})

    def refuse(self, request_id: Any, message: str) -> None:
        self.send({"id": request_id,
                   "error": {"code": -32601, "message": message}})

    def receive(self, timeout: float) -> dict | None:
        """The next message from Codex; None when none came in TIMEOUT."""

        try:
            found = self.lines.get(timeout=max(timeout, 0))
        except queue.Empty:
            return None
        if found is None:
            self.lines.put(None)  # so every later read sees it too
            raise CodexError(self._gone())
        return found

    def call(self, method: str, params: dict | None = None,
             on_other: Callable[[dict], None] | None = None) -> Any:
        """METHOD's result. What else arrives meanwhile goes to ON_OTHER,
        or is answered as nothing this asks for."""

        self.next_id += 1
        mine = self.next_id
        self.send({"id": mine, "method": method, "params": params or {}})
        deadline = time.monotonic() + ANSWER_SECONDS
        while True:
            found = self.receive(deadline - time.monotonic())
            if found is None:
                raise CodexError(f"Codex did not answer {method} in time.")
            if found.get("id") == mine and "method" not in found:
                if found.get("error"):
                    error = found["error"]
                    raise CodexError(str(error.get("message") or error))
                return found.get("result")
            (on_other or self.unasked)(found)

    def unasked(self, found: dict) -> None:
        """A request of Codex's that nothing here is waiting on: turned
        down, so it never waits on us."""

        if "id" in found and "method" in found:
            decline(self, found)

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def decline(server: AppServer, found: dict) -> None:
    """Codex asking for something only a person may allow (a command, a
    file change, more permissions): no, since Flash is the one that asks
    them, and its tools are the only ones offered."""

    method = str(found.get("method") or "")
    if method in ("item/commandExecution/requestApproval",
                  "item/fileChange/requestApproval",
                  "execCommandApproval", "applyPatchApproval"):
        server.respond(found["id"], {"decision": "decline"})
    elif method == "item/permissions/requestApproval":
        server.respond(found["id"], {"permissions": {}})
    elif method == "mcpServer/elicitation/request":
        server.respond(found["id"], {"action": "decline"})
    elif method == "item/tool/requestUserInput":
        server.respond(found["id"], {"answers": {}})
    else:
        server.refuse(found["id"], f"{method} is not something Flash does.")


def start(argv: list[str] | None = None) -> AppServer:
    server = AppServer(argv)
    server.call("initialize", {
        "clientInfo": {"name": "flash", "title": "Flash",
                       "version": VERSION},
        # Handing Codex Flash's tools (dynamicTools) and leaving its own
        # out (environments) are both still marked experimental.
        "capabilities": {"experimentalApi": True},
    })
    server.notify("initialized")
    return server


# On the plan, and only on the plan ------------------------------------------


def _when(seconds: Any) -> str:
    try:
        stamp = float(seconds)
    except (TypeError, ValueError):
        return ""
    return time.strftime(" (until %a %H:%M)", time.localtime(stamp))


def check_plan(server: AppServer) -> dict:
    """Accept API-key login; check included usage for ChatGPT accounts."""

    found = server.call("account/read", {}) or {}
    account = found.get("account") or {}
    if account.get("type") == "apiKey":
        return account
    if account.get("type") != "chatgpt":
        raise CodexError(NOT_SIGNED_IN, 401)
    if account.get("planType") in BILLED_BY_USE:
        raise CodexError(BILLED_PLAN, 402)

    limits = server.call("account/rateLimits/read", {}) or {}
    allowed = limits.get("ordinaryUsageAllowed")
    snapshots = [limits.get("rateLimits") or {}]
    snapshots += list((limits.get("rateLimitsByLimitId") or {}).values())
    full = [
        window for snap in snapshots if isinstance(snap, dict)
        for window in (snap.get("primary"), snap.get("secondary"))
        if isinstance(window, dict)
        and int(window.get("usedPercent") or 0) >= 100
    ]
    reached = any(isinstance(snap, dict) and snap.get("rateLimitReachedType")
                  for snap in snapshots)
    if allowed is False or (allowed is None and (full or reached)):
        resets = max((w.get("resetsAt") or 0 for w in full), default=0)
        raise CodexError(
            "Your ChatGPT plan's included usage is used up for now"
            f"{_when(resets) if resets else ''}. Flash does not go on with "
            "credits or anything billed, so these models are paused until "
            "then. Pick another model with /model meanwhile.", 429)
    return account


# Models ----------------------------------------------------------------------


def list_models(server: AppServer) -> list[dict]:
    found: list[dict] = []
    cursor = None
    for _ in range(20):
        page = server.call("model/list", {"cursor": cursor}) or {}
        found += [m for m in page.get("data") or [] if isinstance(m, dict)]
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return [m for m in found if not m.get("hidden")]


def efforts_of(model: dict | None) -> list[str]:
    return [str(e.get("reasoningEffort")) for e in
            (model or {}).get("supportedReasoningEfforts") or []
            if isinstance(e, dict) and e.get("reasoningEffort")]


def shown(model: dict) -> dict:
    capabilities = ["completion", "tools"]
    if "image" in (model.get("inputModalities") or ["text", "image"]):
        capabilities.append("vision")
    if efforts_of(model):
        capabilities.append("thinking")
    return {"name": model.get("id") or model.get("model"),
            "capabilities": capabilities}


def _cached() -> list[dict] | None:
    try:
        kept = json.loads((CACHE / "models.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kept, dict) or not isinstance(kept.get("models"), list):
        return None
    if time.time() - float(kept.get("at") or 0) > MODELS_TTL:
        return None
    return kept["models"]


def _keep(models: list[dict]) -> None:
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / "models.json").write_text(
            json.dumps({"at": time.time(), "models": models}),
            encoding="utf-8")
    except OSError:
        pass


def models(argv: list[str] | None = None) -> None:
    cached = _cached()
    if cached is not None:
        say({"models": [shown(m) for m in cached]})
        return
    server = start(argv)
    try:
        check_plan(server)
        found = list_models(server)
    finally:
        server.close()
    _keep(found)
    say({"models": [shown(m) for m in found]})


def find(found: list[dict], name: str) -> dict | None:
    return next((m for m in found if name in (m.get("id"), m.get("model"))),
                None)


def effort_for(ask: dict, model: dict | None) -> str | None:
    """How hard the model thinks: as hard as it can when Flash asks it to
    think harder, else the setting, else the model's own default (None)."""

    have = efforts_of(model)
    think = ask.get("think")
    wanted = setting("effort").lower()
    if isinstance(think, str) and think.lower() in EFFORTS:
        wanted = think.lower()
    elif think is True:
        wanted = "max"
    if wanted not in EFFORTS:
        return None
    if not have:
        return wanted if model is None else None
    if wanted in have:
        return wanted
    rank = EFFORTS.index(wanted)
    return min(have, key=lambda e: abs(EFFORTS.index(e) - rank)
               if e in EFFORTS else 99)


# The conversation, as Codex takes it -----------------------------------------


def _media_type(data: str) -> str:
    head = base64.b64decode(data[:24] + "=" * (-len(data[:24]) % 4))
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def image_urls(message: dict) -> list[str]:
    urls = []
    for image in message.get("images") or []:
        image = str(image)
        if image.startswith("data:"):
            urls.append(image)
            continue
        try:
            urls.append(f"data:{_media_type(image)};base64,{image}")
        except ValueError:
            continue
    return urls


def text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict))
    return str(content or "")


def _arguments(raw: Any) -> str:
    return raw if isinstance(raw, str) else json.dumps(raw or {})


def _said(role: str, text: str, urls: list[str] = ()) -> dict:
    kind = "output_text" if role == "assistant" else "input_text"
    content: list[dict] = [{"type": kind, "text": text}] if text else []
    content += [{"type": "input_image", "image_url": url} for url in urls]
    return {"type": "message", "role": role, "content": content}


def split(messages: list) -> tuple[str, list[dict], list[dict]]:
    """MESSAGES as Codex takes them: the instructions (the system messages
    the conversation opens with), the history to put into the thread as
    it was, and the input that starts the turn."""

    messages = [m for m in messages or [] if isinstance(m, dict)]
    lead = 0
    while lead < len(messages) and messages[lead].get("role") == "system":
        lead += 1
    instructions = "\n\n".join(text_of(m) for m in messages[:lead]
                               if text_of(m).strip())
    rest = messages[lead:]

    # The last user message starts the turn; after a tool's result, a note
    # does, since a turn has to start with something.
    last = rest[-1] if rest else None
    opening = last if last and last.get("role") == "user" else None
    history_of = rest[:-1] if opening else rest

    history: list[dict] = []
    waiting: list[str] = []  # ids of tool calls not yet answered
    made = 0
    for message in history_of:
        role = message.get("role")
        text = text_of(message)
        if role == "assistant":
            if text.strip():
                history.append(_said("assistant", text))
            for call in message.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                made += 1
                call_id = str((call or {}).get("id") or f"call_{made}")
                waiting.append(call_id)
                history.append({
                    "type": "function_call", "call_id": call_id,
                    "name": str(function.get("name") or ""),
                    "arguments": _arguments(function.get("arguments")),
                })
        elif role == "tool":
            urls = image_urls(message)
            if waiting:
                history.append({"type": "function_call_output",
                                "call_id": waiting.pop(0), "output": text})
            else:
                history.append(_said("user", f"{NOTE} A tool's result:\n"
                                     + text))
            if urls:
                history.append(_said(
                    "user", f"{NOTE} The pictures that tool gave:", urls))
        elif role == "system":
            history.append(_said("developer", text))
        else:
            history.append(_said("user", text, image_urls(message)))
    # A call Flash never answered still needs an answer to follow it.
    for call_id in waiting:
        history.append({"type": "function_call_output", "call_id": call_id,
                        "output": "The tool was not run."})

    if opening:
        opening_input = []
        if text_of(opening):
            opening_input.append({"type": "text", "text": text_of(opening)})
        opening_input += [{"type": "image", "url": url}
                          for url in image_urls(opening)]
        if not opening_input:
            opening_input = [{"type": "text", "text": "(nothing)"}]
    else:
        opening_input = [{"type": "text", "text": CONTINUE}]
    return instructions, history, opening_input


def dynamic_tools(tools: list | None) -> list[dict]:
    found = []
    for tool in tools or []:
        function = (tool or {}).get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        found.append({
            "type": "function",
            "name": name,
            "description": str(function.get("description") or ""),
            "inputSchema": function.get("parameters")
            or {"type": "object", "properties": {}},
        })
    return found


# A turn ----------------------------------------------------------------------


class Out:
    """The answer to Flash: in pieces as it is written when Flash asked
    for a stream, else at the end."""

    def __init__(self, stream: bool) -> None:
        self.stream = stream
        self.content: list[str] = []
        self.thinking: list[str] = []
        self.calls: list[dict] = []
        self.counts: dict = {}

    def text(self, piece: str) -> None:
        if piece:
            self.content.append(piece)
            if self.stream:
                say({"message": {"role": "assistant", "content": piece},
                     "done": False})

    def thought(self, piece: str) -> None:
        if piece:
            self.thinking.append(piece)
            if self.stream:
                say({"message": {"role": "assistant", "content": "",
                                 "thinking": piece}, "done": False})

    def finish(self) -> None:
        message: dict = {"role": "assistant",
                         "content": "" if self.stream
                         else "".join(self.content)}
        if self.thinking and not self.stream:
            message["thinking"] = "".join(self.thinking)
        if self.calls:
            message["tool_calls"] = self.calls
        say({"message": message, "done": True, **self.counts})


def _failure(turn: dict, said: str) -> CodexError:
    error = turn.get("error") or {}
    info = error.get("codexErrorInfo")
    message = str(error.get("message") or said or "The turn failed.")
    if info in ("usageLimitExceeded", "rateLimitExceeded"):
        return CodexError(
            "Your ChatGPT plan's included usage ran out during this answer. "
            "Flash does not go on with credits or anything billed. "
            f"Codex said: {message}", 429)
    if info == "unauthorized":
        return CodexError("Codex's sign-in has expired. Run `codex login` "
                          "again.", 401)
    if info == "modelNotFound":
        return CodexError(f"Your plan does not offer that model. {message}",
                          404)
    if info == "contextWindowExceeded":
        return CodexError("The conversation is too long for this model. "
                          "Compact it (/compact) or start a new one.", 400)
    return CodexError(message)


def run_turn(server: AppServer, ask: dict, out: Out) -> None:
    instructions, history, opening = split(ask.get("messages") or [])
    wanted = str(ask.get("model") or "")
    model = None
    if ask.get("think") or setting("effort").lower() in EFFORTS:
        try:
            model = find(list_models(server), wanted)
        except CodexError:
            model = None

    params: dict = {
        "model": wanted or None,
        "cwd": os.getcwd(),
        "ephemeral": True,
        # Flash asks before anything runs; Codex is to run nothing itself.
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "environments": [],
        "dynamicTools": dynamic_tools(ask.get("tools")),
    }
    params["baseInstructions"] = "\n\n".join(
        part for part in (instructions, TOOL_ENVIRONMENT) if part)
    thread = (server.call("thread/start", params) or {}).get("thread") or {}
    thread_id = thread.get("id")
    if not thread_id:
        raise CodexError("Codex did not start a thread.")
    if history:
        server.call("thread/inject_items",
                    {"threadId": thread_id, "items": history})

    turn_params: dict = {"threadId": thread_id, "input": opening,
                         "summary": "auto" if ask.get("think") is not False
                         else "none"}
    effort = effort_for(ask, model)
    if effort:
        turn_params["effort"] = effort

    state = {"turn": "", "done": None, "said": "", "streamed": set(),
             "until": None, "gap": False}

    def other(found: dict) -> None:
        handle(server, found, out, state)

    started = server.call("turn/start", turn_params, on_other=other) or {}
    state["turn"] = (started.get("turn") or {}).get("id") or state["turn"]

    quiet_until = time.monotonic() + QUIET_SECONDS
    while state["done"] is None:
        wait = quiet_until - time.monotonic()
        if state["until"] is not None:
            wait = min(wait, state["until"] - time.monotonic())
        found = server.receive(wait)
        if found is not None:
            quiet_until = time.monotonic() + QUIET_SECONDS
            handle(server, found, out, state)
            continue
        if state["until"] is not None and time.monotonic() >= state["until"]:
            # The tool calls go to Flash; this turn ends here, unanswered.
            try:
                server.call("turn/interrupt",
                            {"threadId": thread_id, "turnId": state["turn"]},
                            on_other=lambda f: None)
            except CodexError:
                pass
            return
        if time.monotonic() >= quiet_until:
            raise CodexError("Codex went quiet in the middle of the answer.")

    turn = state["done"]
    if turn.get("status") == "failed" and not out.calls:
        raise _failure(turn, state["said"])


def handle(server: AppServer, found: dict, out: Out, state: dict) -> None:
    method = str(found.get("method") or "")
    params = found.get("params") or {}
    if "id" in found and method:
        if method == "item/tool/call":
            out.calls.append({"function": {
                "name": str(params.get("tool") or ""),
                "arguments": params.get("arguments") or {},
            }})
            if state["until"] is None:
                state["until"] = time.monotonic() + MORE_CALLS_SECONDS
        else:
            decline(server, found)
        return
    if method == "turn/started":
        state["turn"] = (params.get("turn") or {}).get("id") or state["turn"]
    elif method == "item/agentMessage/delta":
        state["streamed"].add(params.get("itemId"))
        _text(out, state, str(params.get("delta") or ""))
    elif method in ("item/reasoning/summaryTextDelta",
                    "item/reasoning/textDelta"):
        out.thought(str(params.get("delta") or ""))
    elif method == "item/completed":
        item = params.get("item") or {}
        if item.get("type") == "agentMessage":
            if item.get("id") not in state["streamed"]:
                _text(out, state, str(item.get("text") or ""))
            state["gap"] = True
    elif method == "thread/tokenUsage/updated":
        total = (params.get("tokenUsage") or {}).get("total") or {}
        out.counts = {"prompt_eval_count": total.get("inputTokens"),
                      "eval_count": total.get("outputTokens")}
    elif method == "error":
        if not params.get("willRetry"):
            state["said"] = str((params.get("error") or {}).get("message")
                                or "")
    elif method == "turn/completed":
        state["done"] = params.get("turn") or {}


def _text(out: Out, state: dict, piece: str) -> None:
    """PIECE of what the model says. Its messages one after another read
    as paragraphs."""

    if not piece:
        return
    if state["gap"] and out.content:
        out.text("\n\n")
    state["gap"] = False
    out.text(piece)


def chat(ask: dict, argv: list[str] | None = None) -> None:
    out = Out(bool(ask.get("stream")))
    server = start(argv)
    try:
        check_plan(server)
        run_turn(server, ask, out)
    finally:
        server.close()
    out.finish()


def main() -> None:
    ask = json.load(sys.stdin)
    try:
        if ask.get("action") == "models":
            models()
        elif ask.get("action") == "chat":
            chat(ask)
        else:
            say({"error": f"unknown action {ask.get('action')!r}"})
    except CodexError as exc:
        say({"error": exc.message, "status": exc.status})
    except Exception as exc:  # noqa: BLE001 -- Flash wants JSON, not a trace
        say({"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
