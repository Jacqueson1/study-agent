"""Phase 6, step 2: chat agent. The LLM picks tools; store.py does the work.

Run (from study-agent/):  uv run python src/agent.py
Needs Ollama running and a tool-capable model (default qwen2.5:7b).
"""
import inspect
import json
import sys
from datetime import date, timedelta

import ollama

import store
from scheduler import DAYS

DEFAULT_MODEL = "qwen2.5:7b"
MAX_STEPS = 6          # tool-call rounds per user message
MAX_BLOCKS_SHOWN = 40  # keep tool output small for the model


# ---------- tool definitions ----------
def _tool(name: str, description: str, props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []},
    }}


_ID = {"type": "string", "description": "Block id from get_schedule, e.g. B032"}
_DATE = {"type": "string", "description": "Date as YYYY-MM-DD"}
_TIME = {"type": "string", "description": "Time as HH:MM, 24-hour"}

TOOLS = [
    _tool("get_schedule", "List scheduled blocks. Filter by date, semester week number, or subject code. "
          "Always call this before referring to a block id.",
          {"day": _DATE, "week": {"type": "integer", "description": "Semester week number"},
           "subject": {"type": "string", "description": "Subject code, e.g. DES3103"}}),
    _tool("move_block", "Move one study block to a new date and start time. The block keeps its length "
          "and becomes pinned.", {"block_id": _ID, "new_date": _DATE, "new_start": _TIME},
          ["block_id", "new_date", "new_start"]),
    _tool("add_event", "Add a personal event (meeting, trip). Study is planned around it.",
          {"day": _DATE, "start": _TIME, "end": _TIME, "title": {"type": "string"}},
          ["day", "start", "end", "title"]),
    _tool("delete_event", "Delete a personal event added with add_event.", {"block_id": _ID}, ["block_id"]),
    _tool("pin_block", "Pin a block so re-planning keeps it where it is.", {"block_id": _ID}, ["block_id"]),
    _tool("unpin_block", "Unpin a block so re-planning may move it.", {"block_id": _ID}, ["block_id"]),
    _tool("clear_pins", "Unpin all study blocks."),
    _tool("block_day", "Mark a day as no-study (e.g. sick, trip). Its study work is re-planned onto other days.",
          {"day": _DATE}, ["day"]),
    _tool("unblock_day", "Allow study again on a previously blocked day.", {"day": _DATE}, ["day"]),
    _tool("replan", "Rebuild the schedule from today. Pinned blocks and blocked days are kept."),
    _tool("undo", "Undo the last change."),
]

FUNCS = {n: getattr(store, n) for n in (
    "get_schedule", "move_block", "add_event", "delete_event", "pin_block", "unpin_block",
    "clear_pins", "block_day", "unblock_day", "replan", "undo")}
READ_ONLY = {"get_schedule"}
PREVIEWABLE = {"move_block", "add_event"}  # have dry_run support in store.py

DECLINED = {"ok": False, "error": "The user declined this change. Do not retry it; ask what they want instead."}


# ---------- running a tool ----------
CONFIRM_TEXT = {
    "delete_event": "Delete event {block_id}",
    "pin_block": "Pin block {block_id} (re-planning will keep it)",
    "unpin_block": "Unpin block {block_id} (re-planning may move it)",
    "clear_pins": "Unpin all study blocks",
    "block_day": "Block {day} (no study) and re-plan its work onto other days",
    "unblock_day": "Unblock {day} and re-plan",
    "replan": "Re-plan the schedule from today (pinned blocks and blocked days are kept)",
    "undo": "Undo the last change",
}


def _clean(name: str, args: dict):
    """-> (function, cleaned args, error result or None)."""
    fn = FUNCS.get(name)
    if fn is None:
        return None, None, {"ok": False, "error": f"Unknown tool '{name}'. Available: {', '.join(FUNCS)}."}
    allowed = set(inspect.signature(fn).parameters) - {"dry_run", "plan_from", "_record_undo"}
    args = {k: (v.strip() if isinstance(v, str) else v) for k, v in (args or {}).items() if k in allowed}
    if "week" in args:
        try:
            args["week"] = int(args["week"])
        except (TypeError, ValueError):
            return None, None, {"ok": False, "error": "week must be a number."}
    return fn, args, None


def _preview(name: str, fn, args: dict):
    """-> (error result or None, text to confirm or None). Read-only tools need no confirmation."""
    try:
        if name in PREVIEWABLE:
            p = fn(**args, dry_run=True)
            return (p, None) if not p["ok"] else (None, p["preview"])
    except TypeError as e:
        return {"ok": False, "error": f"Bad arguments for {name}: {e}"}, None
    if name in READ_ONLY:
        return None, None
    try:
        return None, CONFIRM_TEXT[name].format(**args)
    except (KeyError, IndexError):
        return None, f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})"


def _execute(name: str, fn, args: dict) -> dict:
    try:
        result = fn(**args)
    except TypeError as e:  # missing or wrong arguments
        return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
    # keep the model's context small
    if name == "replan" and result.get("ok"):
        result = {"ok": True, "message": result["message"], "unplaced": result["unplaced"][:8]}
    if name == "get_schedule" and result.get("ok") and result["count"] > MAX_BLOCKS_SHOWN:
        result["blocks"] = result["blocks"][:MAX_BLOCKS_SHOWN]
        result["note"] = f"Showing first {MAX_BLOCKS_SHOWN} of {result['count']}. Filter by week or day."
    return result


# ---------- prompt ----------
def system_prompt() -> str:
    cfg = store._cfg()
    today = date.today()
    start = date.fromisoformat(cfg["semester"]["start_date"])
    cal = "\n".join(f"  {d:%a %Y-%m-%d} (week {(d - start).days // 7 + 1})"
                    for d in (today + timedelta(days=i) for i in range(15)))
    return f"""You edit a student's study schedule using tools. Today is {today:%A %Y-%m-%d}.
The semester starts {start} and has {cfg['semester']['weeks']} weeks (week 1 starts on that date).

Next 15 days:
{cal}

Rules:
- Never guess block ids. Call get_schedule first, then use the exact id.
- Convert words like "tomorrow" or "next Thursday" to YYYY-MM-DD using the calendar above.
- To change anything, call the tool. Do not just say you did it.
- If a tool returns an error, read it and fix the arguments, or tell the user why it is not possible.
- If the user declines a change, do not retry it.
- Keep replies short."""


# ---------- chat ----------
class Chat:
    """One conversation. Works for the terminal and for Streamlit.

    send(text) / resolve(approve) return one of:
      {"type": "text", "text": ...}      final answer
      {"type": "confirm", "text": ...}   a change waits for approve/decline -> call resolve()
    """

    def __init__(self, model: str):
        self.model = model
        self.day = date.today()
        self.messages: list = [{"role": "system", "content": system_prompt()}]
        self.display: list[tuple[str, str]] = []  # (role, text) for the UI
        self.queue: list = []                     # tool calls not handled yet
        self.pending: dict | None = None          # change waiting for confirmation

    def send(self, text: str) -> dict:
        self.display.append(("user", text))
        self.messages.append({"role": "user", "content": text})
        return self._advance()

    def resolve(self, approve: bool) -> dict:
        p, self.pending = self.pending, None
        if p is None:
            return {"type": "text", "text": ""}
        result = _execute(p["name"], p["fn"], p["args"]) if approve else DECLINED
        self._record(p["name"], result)
        return self._advance()

    def note(self, text: str) -> None:
        """Tell the model about something that happened outside the chat (e.g. Undo button)."""
        self.messages.append({"role": "user", "content": f"[note] {text}"})
        self.display.append(("tool", text))

    def _record(self, name: str, result: dict) -> None:
        self.messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result)})
        self.display.append(("tool", f"{name}: " + (result.get("message") or result.get("error")
                                                       or ("done" if result.get("ok") else "failed"))))

    def _advance(self) -> dict:
        for _ in range(MAX_STEPS):
            if not self.queue:
                try:
                    msg = ollama.chat(model=self.model, messages=self.messages, tools=TOOLS).message
                except Exception as e:  # noqa: BLE001  (Ollama not running, model missing...)
                    return self._say(f"Could not reach the model: {e}")
                self.messages.append(msg)
                if not msg.tool_calls:
                    return self._say(msg.content or "")
                self.queue = list(msg.tool_calls)
            while self.queue:
                call = self.queue.pop(0)
                name, raw = call.function.name, dict(call.function.arguments or {})
                fn, args, err = _clean(name, raw)
                if err is None:
                    err, text = _preview(name, fn, args)
                    if err is None and text:
                        self.pending = {"name": name, "fn": fn, "args": args, "text": text}
                        return {"type": "confirm", "text": text}
                    if err is None:
                        err = _execute(name, fn, args)
                self._record(name, err)
        self.queue = []
        return self._say("I stopped after too many steps. Please try a simpler request.")

    def _say(self, text: str) -> dict:
        self.display.append(("assistant", text))
        return {"type": "text", "text": text}


def ask_terminal(text: str) -> bool:
    return input(f"\n  Apply this change?  {text}\n  [y/N] ").strip().lower() in ("y", "yes")


def main():
    model = store._cfg().get("model", DEFAULT_MODEL)
    try:
        ollama.show(model)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Cannot use model '{model}': {e}\nIs Ollama running? Try: ollama pull {model}")
    if not store.load_schedule():
        print("No schedule yet. Say 'replan' to build one, or generate it in the app first.")

    chat = Chat(model)
    print(f"Study agent ({model}). Type 'exit' to quit.")
    while True:
        text = input("\nYou: ").strip()
        if text.lower() in ("exit", "quit"):
            break
        if not text:
            continue
        out = chat.send(text)
        while out["type"] == "confirm":
            out = chat.resolve(ask_terminal(out["text"]))
        print("\nAgent:", out["text"])


if __name__ == "__main__":
    main()
