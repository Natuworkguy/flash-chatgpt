# ChatGPT for Flash

A [Flash](https://github.com/Natuworkguy/Flash) extension that adds
ChatGPT's models as a model provider, **using Codex’s current login**. ChatGPT logins use included plan usage;
API-key logins use API billing. Once it is installed, they
are picked like any other model:

```text
/model @chatgpt/gpt-5.5
```

They also show in the web UI's model menu, can be a spark's model, and
run sub-agents. Tools, images, thinking and streaming in the web UI all
work.

## Install

You need [Codex](https://github.com/openai/codex), signed in with ChatGPT or configured with an API key. This extension
talks to the models through Codex.

```bash
npm install -g @openai/codex     # or: brew install codex
codex login                      # choose Sign in with ChatGPT
```

Then, in Flash:

```text
/extension install github@Natuworkguy/flash-chatgpt
```

Or from a folder, to try it out: `/extension install path@/path/to/flash-chatgpt`.

If `codex` is installed somewhere that is not on your PATH, set where it
is in the extension's settings: on Flash's Extensions page, **Settings**
on chatgpt, then **Codex program**. In the terminal:

```text
/extension set chatgpt codex /path/to/codex
```

## Authentication and billing

API-key logins are accepted and use API billing. No ChatGPT plan or
included-usage checks apply to API-key requests. Unsigned users are refused.

For ChatGPT logins, every request is checked before anything is sent:
- **A plan with included usage.** Workspaces billed by use are turned
  away.
- **Usage left.** Codex is asked whether your plan's included usage can
  be used right now. Once it is used up, the request is refused with the
  time it comes back, so credits you have bought for Codex are never
  spent here. Pick another model with `/model` meanwhile.

If the limit runs out in the middle of an answer, Flash says so and
stops there.

## Models

`/model` lists what your plan offers, as Codex lists them (the ones it
hides from its own picker are left out). The list is kept for ten
minutes. Every name is the model's id: `@chatgpt/<id>`.

## Settings

| Setting | Meaning |
| --- | --- |
| Effort | How hard the model thinks: `model default`, `low`, `medium`, `high` or `xhigh`. A reply Flash asks to think harder (Think harder, or Flash deciding a message needs it) goes as hard as the model does. |
| Codex program | Where `codex` is, if it is not on your PATH. |

## How it works

- **Flash's tools are the only tools.** Codex's own shell and file
  editing are switched off for these threads. Everything the model does
  goes through Flash's tools, and Flash asks before running anything as
  it always does.
- **Each request is a fresh thread that is not kept.** The conversation
  so far goes into it as it was (messages, tool calls and their results,
  pictures), and one turn runs. When the model calls tools, they go back
  to Flash, which runs them and asks again with the results.
- **Thinking** shows where Flash shows a model's thinking, as the
  model's summary of it.
- Only Python's standard library is used, so nothing is installed into
  Flash.

Flash's tools and keeping Codex's own out rely on parts of Codex's
app-server that it still marks experimental, so a Codex update could
change them. Your conversations go to OpenAI through Codex, so `/stats`
never counts these models as running on your own machine.

## Development

```bash
python -m pytest
```

The tests run against a stand-in for `codex app-server` and never reach
OpenAI.

MIT licensed. See [LICENSE](LICENSE).
