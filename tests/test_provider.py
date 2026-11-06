"""Tests for provider.py, against a stand-in for codex app-server."""

import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import provider  # noqa: E402,RUF100

FAKE = str(Path(__file__).resolve().parent / "fake_codex.py")
CHATGPT = {"type": "chatgpt", "email": "me@example.com", "planType": "plus"}
MODELS = [
    {"id": "gpt-big", "model": "gpt-big", "displayName": "Big",
     "description": "", "hidden": False, "isDefault": True,
     "inputModalities": ["text", "image"],
     "defaultReasoningEffort": "medium",
     "supportedReasoningEfforts": [
         {"reasoningEffort": e, "description": ""}
         for e in ("low", "medium", "high", "xhigh")]},
    {"id": "gpt-hidden", "model": "gpt-hidden", "displayName": "Hidden",
     "description": "", "hidden": True, "isDefault": False,
     "defaultReasoningEffort": "medium", "supportedReasoningEfforts": []},
]


def delta(text, item="a1"):
    return {"method": "item/agentMessage/delta", "params": {
        "threadId": "t1", "turnId": "u1", "itemId": item, "delta": text}}


def message_done(text, item="a1"):
    return {"method": "item/completed", "params": {
        "threadId": "t1", "turnId": "u1", "completedAtMs": 0,
        "item": {"type": "agentMessage", "id": item, "text": text}}}


def usage(inputs, outputs):
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": "t1", "turnId": "u1", "tokenUsage": {
            "total": {"inputTokens": inputs, "outputTokens": outputs,
                      "cachedInputTokens": 0, "reasoningOutputTokens": 0,
                      "totalTokens": inputs + outputs},
            "last": {"inputTokens": inputs, "outputTokens": outputs,
                     "cachedInputTokens": 0, "reasoningOutputTokens": 0,
                     "totalTokens": inputs + outputs}}}}


def completed(status="completed", error=None):
    return {"method": "turn/completed", "params": {
        "threadId": "t1", "turn": {"id": "u1", "items": [],
                                   "status": status, "error": error}}}


def tool_call(name, arguments, number=1):
    return {"id": 900 + number, "method": "item/tool/call", "params": {
        "threadId": "t1", "turnId": "u1", "callId": f"c{number}",
        "tool": name, "arguments": arguments}}


@pytest.fixture
def codex(tmp_path, monkeypatch):
    """Sets the scenario; gives back what codex was sent."""

    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.setattr(provider, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(provider, "MORE_CALLS_SECONDS", 0.05)
    for name in ("EFFORT", "CODEX"):
        monkeypatch.delenv(f"FLASH_SETTING_{name}", raising=False)

    def scenario(**given):
        given.setdefault("account", CHATGPT)
        given.setdefault("models", MODELS)
        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(given), encoding="utf-8")
        monkeypatch.setenv("FAKE_CODEX", str(path))

    def sent():
        if not log.exists():
            return []
        return [json.loads(line) for line in
                log.read_text(encoding="utf-8").splitlines()]

    scenario.sent = sent
    return scenario


def ask(messages, **more):
    return {"action": "chat", "model": "gpt-big", "messages": messages,
            "stream": False, **more}


def answers(capsys):
    return [json.loads(line) for line in
            capsys.readouterr().out.splitlines() if line.strip()]


def run(request):
    provider.chat(request, argv=[sys.executable, FAKE])


def method(sent, name):
    return [m for m in sent if m.get("method") == name]


class TestOnThePlan:
    def test_an_api_key_sign_in_sends_nothing(self, codex, capsys):
        codex(account={"type": "apiKey"}, events=[completed()])

        with pytest.raises(provider.CodexError) as caught:
            run(ask([{"role": "user", "content": "hi"}]))

        assert "ChatGPT" in caught.value.message
        assert caught.value.status == 401
        assert not method(codex.sent(), "thread/start")
        assert not method(codex.sent(), "turn/start")

    def test_not_signed_in_sends_nothing(self, codex):
        codex(account=None, events=[completed()])

        with pytest.raises(provider.CodexError):
            run(ask([{"role": "user", "content": "hi"}]))

        assert not method(codex.sent(), "turn/start")

    def test_a_plan_billed_by_use_is_turned_away(self, codex):
        codex(account={**CHATGPT,
                       "planType": "self_serve_business_usage_based"})

        with pytest.raises(provider.CodexError) as caught:
            run(ask([{"role": "user", "content": "hi"}]))

        assert "billed by use" in caught.value.message
        assert not method(codex.sent(), "turn/start")

    def test_used_up_it_waits_rather_than_spend_credits(self, codex):
        codex(limits={"ordinaryUsageAllowed": False, "rateLimits": {
            "credits": {"hasCredits": True, "unlimited": False},
            "primary": {"usedPercent": 100, "resetsAt": 4102444800}}})

        with pytest.raises(provider.CodexError) as caught:
            run(ask([{"role": "user", "content": "hi"}]))

        assert caught.value.status == 429
        assert "credits" in caught.value.message
        assert "until" in caught.value.message
        assert not method(codex.sent(), "turn/start")

    def test_a_full_window_counts_when_codex_cannot_say(self, codex):
        codex(limits={"ordinaryUsageAllowed": None, "rateLimits": {
            "secondary": {"usedPercent": 100}}})

        with pytest.raises(provider.CodexError):
            run(ask([{"role": "user", "content": "hi"}]))

    def test_running_out_mid_answer_says_so(self, codex):
        codex(events=[completed("failed", {
            "message": "You've hit your usage limit.",
            "codexErrorInfo": "usageLimitExceeded"})])

        with pytest.raises(provider.CodexError) as caught:
            run(ask([{"role": "user", "content": "hi"}]))

        assert caught.value.status == 429
        assert "usage limit" in caught.value.message


class TestAnswers:
    def test_a_reply_with_its_token_counts(self, codex, capsys):
        codex(events=[delta("Hel"), delta("lo."), message_done("Hello."),
                      usage(120, 4), completed()])

        run(ask([{"role": "system", "content": "Be Flash."},
                 {"role": "user", "content": "hi"}]))

        [answer] = answers(capsys)
        assert answer["message"]["content"] == "Hello."
        assert answer["prompt_eval_count"] == 120
        assert answer["eval_count"] == 4
        assert answer["done"] is True

    def test_streamed_in_pieces(self, codex, capsys):
        codex(events=[delta("Hel"), delta("lo."), message_done("Hello."),
                      completed()])

        run(ask([{"role": "user", "content": "hi"}], stream=True))

        said = answers(capsys)
        assert [a["message"]["content"] for a in said] == ["Hel", "lo.", ""]
        assert said[-1]["done"] is True

    def test_messages_one_after_another_are_paragraphs(self, codex, capsys):
        codex(events=[message_done("Looking.", "a1"),
                      delta("Found it.", "a2"), message_done("", "a2"),
                      completed()])

        run(ask([{"role": "user", "content": "hi"}]))

        assert answers(capsys)[0]["message"]["content"] == (
            "Looking.\n\nFound it.")

    def test_thinking_comes_through(self, codex, capsys):
        codex(events=[
            {"method": "item/reasoning/summaryTextDelta", "params": {
                "threadId": "t1", "turnId": "u1", "itemId": "r1",
                "summaryIndex": 0, "delta": "Weighing it."}},
            message_done("Yes."), completed()])

        run(ask([{"role": "user", "content": "hi"}]))

        assert answers(capsys)[0]["message"]["thinking"] == "Weighing it."

    def test_a_failed_turn_is_an_error(self, codex):
        codex(events=[completed("failed", {"message": "it broke"})])

        with pytest.raises(provider.CodexError) as caught:
            run(ask([{"role": "user", "content": "hi"}]))

        assert caught.value.message == "it broke"


class TestTools:
    TOOLS: ClassVar[list] = [{"type": "function", "function": {
        "name": "read", "description": "Read a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}}]

    def test_flash_tools_are_the_only_ones(self, codex, capsys):
        codex(events=[message_done("Ok."), completed()])

        run(ask([{"role": "user", "content": "hi"}], tools=self.TOOLS))

        [start] = method(codex.sent(), "thread/start")
        params = start["params"]
        assert params["environments"] == []
        assert params["approvalPolicy"] == "never"
        assert params["ephemeral"] is True
        assert params["dynamicTools"] == [{
            "type": "function", "name": "read", "description": "Read a file.",
            "inputSchema": self.TOOLS[0]["function"]["parameters"]}]
        [init] = method(codex.sent(), "initialize")
        assert init["params"]["capabilities"]["experimentalApi"] is True

    def test_a_tool_call_goes_back_to_flash(self, codex, capsys):
        codex(events=[delta("Reading."), tool_call("read", {"path": "a.txt"}),
                      tool_call("read", {"path": "b.txt"}, 2)])

        run(ask([{"role": "user", "content": "read them"}],
                tools=self.TOOLS))

        message = answers(capsys)[0]["message"]
        assert message["content"] == "Reading."
        assert message["tool_calls"] == [
            {"function": {"name": "read", "arguments": {"path": "a.txt"}}},
            {"function": {"name": "read", "arguments": {"path": "b.txt"}}},
        ]
        assert method(codex.sent(), "turn/interrupt")

    def test_codex_asking_to_run_something_is_told_no(self, codex, capsys):
        codex(events=[
            {"id": 77, "method": "item/commandExecution/requestApproval",
             "params": {"threadId": "t1", "turnId": "u1", "itemId": "x"}},
            message_done("Fine."), completed()])

        run(ask([{"role": "user", "content": "hi"}]))

        replies = [m for m in codex.sent() if m.get("id") == 77]
        assert replies == [{"id": 77, "result": {"decision": "decline"}}]


class TestTheConversation:
    def test_it_goes_in_as_it_was(self, codex, capsys):
        codex(events=[message_done("Done."), completed()])
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"

        run(ask([
            {"role": "system", "content": "Be Flash."},
            {"role": "user", "content": "what is in a.txt?"},
            {"role": "assistant", "content": "Let me look.", "tool_calls": [
                {"function": {"name": "read", "arguments": {"path": "a"}}}]},
            {"role": "tool", "content": "hello"},
            {"role": "assistant", "content": "It says hello."},
            {"role": "user", "content": "and this?", "images": [png]},
        ]))

        [start] = method(codex.sent(), "thread/start")
        assert start["params"]["baseInstructions"] == "Be Flash."
        [inject] = method(codex.sent(), "thread/inject_items")
        items = inject["params"]["items"]
        assert [i["type"] for i in items] == [
            "message", "message", "function_call", "function_call_output",
            "message"]
        assert items[2]["name"] == "read"
        assert json.loads(items[2]["arguments"]) == {"path": "a"}
        assert items[3]["call_id"] == items[2]["call_id"]
        assert items[3]["output"] == "hello"
        assert items[4]["content"] == [
            {"type": "output_text", "text": "It says hello."}]
        [turn] = method(codex.sent(), "turn/start")
        assert turn["params"]["input"][0] == {"type": "text",
                                              "text": "and this?"}
        assert turn["params"]["input"][1]["url"].startswith(
            "data:image/png;base64,")

    def test_after_a_tool_result_a_note_starts_the_turn(self, codex, capsys):
        codex(events=[message_done("It says hi."), completed()])

        run(ask([
            {"role": "user", "content": "read a"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "read", "arguments": {"path": "a"}}}]},
            {"role": "tool", "content": "hi"},
        ]))

        [inject] = method(codex.sent(), "thread/inject_items")
        assert inject["params"]["items"][-1]["type"] == "function_call_output"
        [turn] = method(codex.sent(), "turn/start")
        assert turn["params"]["input"][0]["text"] == provider.CONTINUE

    def test_harder_thinking_is_the_model_s_highest(self, codex, capsys):
        codex(events=[message_done("Yes."), completed()])

        run(ask([{"role": "user", "content": "hi"}], think=True))

        [turn] = method(codex.sent(), "turn/start")
        assert turn["params"]["effort"] == "xhigh"


class TestModels:
    def test_listed_without_the_hidden_ones(self, codex, capsys):
        codex()

        provider.models(argv=[sys.executable, FAKE])

        [answer] = answers(capsys)
        assert answer["models"] == [{
            "name": "gpt-big",
            "capabilities": ["completion", "tools", "vision", "thinking"]}]

    def test_kept_for_a_while(self, codex, capsys):
        codex()
        provider.models(argv=[sys.executable, FAKE])
        capsys.readouterr()
        codex(models=[])

        provider.models(argv=[sys.executable, FAKE])

        assert answers(capsys)[0]["models"][0]["name"] == "gpt-big"

    def test_listing_needs_the_plan_too(self, codex):
        codex(account={"type": "apiKey"})

        with pytest.raises(provider.CodexError):
            provider.models(argv=[sys.executable, FAKE])


def test_no_codex_says_how_to_get_it(monkeypatch):
    monkeypatch.setenv("FLASH_SETTING_CODEX", "/nowhere/codex")

    with pytest.raises(provider.CodexError) as caught:
        provider.codex_program()

    assert "codex login" in caught.value.message
