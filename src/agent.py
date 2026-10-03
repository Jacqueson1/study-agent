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
def run_tool(name: str, args: dict, confirm) -> dict:
    """Validate, ask the user to confirm changes, run, and shrink the result.

    confirm(text) -> bool is supplied by the caller (terminal now, Streamlit later).
    """
    fn = FUNCS.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool '{name}'. Available: {', '.join(FUNCS)}."}
    allowed = set(inspect.signature(fn).parameters) - {"dry_run", "plan_from", "_record_undo"}
    args = {k: (v.strip() if isinstance(v, str) else v) for k, v in (args or {}).items() if k in allowed}
    if "week" in args:
        try:
            args["week"] = int(args["week"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "week must be a number."}

    try:
        if name in PREVIEWABLE:
            preview = fn(**args, dry_run=True)
            if not preview["ok"]:
                return preview  # the model sees the reason and can correct itself
            if not confirm(preview["preview"]):
                return DECLINED
        elif name not in READ_ONLY:
            if not confirm(f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})"):
                return DECLINED
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
def reply(messages: list, user_text: str, confirm, model: str) -> str:
    """Add the user message, let the model use tools, return its final answer."""
    messages.append({"role": "user", "content": user_text})
    for _ in range(MAX_STEPS):
        resp = ollama.chat(model=model, messages=messages, tools=TOOLS)
        msg = resp.message
        messages.append(msg)
        if not msg.tool_calls:
            return msg.content or ""
        for call in msg.tool_calls:
            result = run_tool(call.function.name, dict(call.function.arguments or {}), confirm)
            messages.append({"role": "tool", "tool_name": call.function.name,
                             "content": json.dumps(result)})
    return "I stopped after too many steps. Please try a simpler request."


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

    messages = [{"role": "system", "content": system_prompt()}]
    print(f"Study agent ({model}). Type 'exit' to quit.")
    while True:
        text = input("\nYou: ").strip()
        if text.lower() in ("exit", "quit"):
            break
        if text:
            print("\nAgent:", reply(messages, text, ask_terminal, model))


if __name__ == "__main__":
    main()
