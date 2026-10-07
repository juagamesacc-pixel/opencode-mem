#!/usr/bin/env python3
"""Standalone stdlib-only MCP server (stdio) for team memory."""
import argparse
import sys
import os
import json
import re
import fcntl
from datetime import datetime, timezone
from contextlib import contextmanager


DEFAULT_ROOT = "/content/opencode-agent2/.memory"


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_now():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def ensure_dirs(root):
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "archive"), exist_ok=True)


@contextmanager
def file_lock(root):
    ensure_dirs(root)
    lock_path = os.path.join(root, ".lock")
    f = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def log_err(msg):
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------- tools ----------------

def do_goal_set(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "user_ask" not in args:
        raise ValueError("missing required argument: user_ask")
    if "expansion" not in args:
        raise ValueError("missing required argument: expansion")
    if "boundaries" not in args:
        raise ValueError("missing required argument: boundaries")
    user_ask = args["user_ask"]
    expansion = args["expansion"]
    boundaries = args["boundaries"]
    approved = args.get("approved", "pending")
    if not isinstance(user_ask, str):
        raise ValueError("user_ask must be a string")
    if not isinstance(expansion, str):
        raise ValueError("expansion must be a string")
    if not isinstance(boundaries, list):
        raise ValueError("boundaries must be an array of string")
    for b in boundaries:
        if not isinstance(b, str):
            raise ValueError("boundaries must be an array of string")
    if approved not in ("pending", "yes", "no"):
        raise ValueError("approved must be one of pending, yes, no")
    ts = iso_now()
    with file_lock(root):
        ensure_dirs(root)
        goal_path = os.path.join(root, "GOAL.md")
        if os.path.exists(goal_path):
            arch = os.path.join(root, "archive", "GOAL-" + compact_now() + ".md")
            os.rename(goal_path, arch)
        lines = []
        lines.append("# GOAL (verbatim \u2014 never paraphrase)")
        lines.append("created: " + ts)
        lines.append("approved: " + approved)
        lines.append("")
        lines.append("## User ask (verbatim)")
        lines.append(user_ask)
        lines.append("")
        lines.append("## Orchestrator expansion (verbatim)")
        lines.append(expansion)
        lines.append("")
        lines.append("## Boundaries")
        for b in boundaries:
            lines.append("- " + b)
        content = "\n".join(lines) + "\n"
        with open(goal_path, "w", encoding="utf-8") as f:
            f.write(content)
    return "GOAL_SET"


def do_goal_get(root, args):
    with file_lock(root):
        goal_path = os.path.join(root, "GOAL.md")
        if not os.path.exists(goal_path):
            return "NO_GOAL_SET"
        return read_text(goal_path)


def do_state_update(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "current_step" not in args:
        raise ValueError("missing required argument: current_step")
    current_step = args["current_step"]
    done = args.get("done", [])
    next_step = args.get("next_step", "")
    evidence = args.get("evidence", [])
    unverified = args.get("unverified", [])
    if not isinstance(current_step, str):
        raise ValueError("current_step must be a string")
    if not isinstance(done, list):
        raise ValueError("done must be an array of string")
    if not isinstance(evidence, list):
        raise ValueError("evidence must be an array of string")
    if not isinstance(unverified, list):
        raise ValueError("unverified must be an array of string")
    if not isinstance(next_step, str):
        raise ValueError("next_step must be a string")
    for v in done:
        if not isinstance(v, str):
            raise ValueError("done must be an array of string")
    for v in evidence:
        if not isinstance(v, str):
            raise ValueError("evidence must be an array of string")
    for v in unverified:
        if not isinstance(v, str):
            raise ValueError("unverified must be an array of string")
    ts = iso_now()

    def fmt_list(vals):
        if not vals:
            return "- (none)"
        return "\n".join("- " + v for v in vals)

    next_txt = next_step if next_step else "- (none)"
    parts = []
    parts.append("# STATE")
    parts.append("updated: " + ts)
    parts.append("## Done")
    parts.append(fmt_list(done))
    parts.append("## Current step")
    parts.append(current_step)
    parts.append("## Next step")
    parts.append(next_txt)
    parts.append("## Evidence")
    parts.append(fmt_list(evidence))
    parts.append("## Unverified")
    parts.append(fmt_list(unverified))
    content = "\n".join(parts) + "\n"
    with file_lock(root):
        ensure_dirs(root)
        final_path = os.path.join(root, "STATE.md")
        tmp_path = os.path.join(root, "STATE.md.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, final_path)
    return "STATE_UPDATED"


def do_context_read(root, args):
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    files = args.get("files", ["all"])
    if not isinstance(files, list):
        raise ValueError("files must be an array of string")
    mapping = {
        "goal": "GOAL.md",
        "state": "STATE.md",
        "decisions": "DECISIONS.md",
        "log": "LOG.md",
        "user": "USER.md",
        "evolution": "EVOLUTION.md",
        "pending": "PENDING.md",
    }
    order_all = ["goal", "state", "decisions", "log", "user", "evolution", "pending"]
    valid = order_all + ["all"]
    for name in files:
        if not isinstance(name, str) or name not in valid:
            raise ValueError("unknown file name: %r. valid names: %s" % (name, ", ".join(valid)))
    if "all" in files:
        files = order_all
    with file_lock(root):
        chunks = []
        for name in files:
            fname = mapping[name]
            fpath = os.path.join(root, fname)
            if os.path.exists(fpath):
                body = read_text(fpath)
            else:
                body = "(missing)"
            chunks.append("===== " + name + " =====\n" + body)
        return "\n\n".join(chunks) + "\n" if chunks else ""


def do_ledger_append(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "kind" not in args:
        raise ValueError("missing required argument: kind")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    kind = args["kind"]
    text = args["text"]
    evidence = args.get("evidence", "")
    agent = args.get("agent", "")
    if kind not in ("log", "decision"):
        raise ValueError("kind must be one of log, decision")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if not isinstance(evidence, str):
        raise ValueError("evidence must be a string")
    if not isinstance(agent, str):
        raise ValueError("agent must be a string")
    ts = iso_now()
    fname = "LOG.md" if kind == "log" else "DECISIONS.md"
    with file_lock(root):
        ensure_dirs(root)
        path = os.path.join(root, fname)
        s = "## " + ts + "[ " + agent + " ] " + text + "\n"
        if evidence:
            s = s + "evidence: " + evidence + "\n"
        s = s + "\n"
        f = open(path, "a", encoding="utf-8")
        try:
            f.write(s)
        finally:
            f.close()
    return "APPENDED"


def do_evolution_search(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "query" not in args:
        raise ValueError("missing required argument: query")
    query = args["query"]
    limit = args.get("limit", 5)
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an int")
    with file_lock(root):
        evo_path = os.path.join(root, "EVOLUTION.md")
        if not os.path.exists(evo_path):
            return "NO_EVOLUTION_ENTRIES"
        raw = read_text(evo_path)
    entries = []
    current = None
    for line in raw.splitlines():
        if line.startswith("## "):
            if current is not None:
                entries.append(current)
            current = line
        else:
            if current is not None:
                current = current + "\n" + line
    if current is not None:
        entries.append(current)
    # drop entries that are empty/whitespace only
    entries = [e for e in entries if e.strip() != ""]
    if not entries:
        return "NO_EVOLUTION_ENTRIES"
    qtokens = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = []
    for idx, entry in enumerate(entries):
        etokens = set(re.findall(r"[a-z0-9]+", entry.lower()))
        score = len(qtokens & etokens)
        scored.append((score, idx, entry))
    scored.sort(key=lambda t: (-t[0], t[1]))
    top = scored[:limit] if limit >= 0 else []
    lines = []
    for score, idx, entry in top:
        lines.append(str(score) + " | " + entry.strip())
    if not lines:
        return "NO_EVOLUTION_ENTRIES"
    return "\n\n".join(lines)


def do_evolution_record(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    for k in ("task", "decision", "reason"):
        if k not in args:
            raise ValueError("missing required argument: " + k)
    task = args["task"]
    decision = args["decision"]
    reason = args["reason"]
    tools_requested = args.get("tools_requested", [])
    outcome = args.get("outcome", "")
    if not isinstance(task, str):
        raise ValueError("task must be a string")
    if decision not in ("allowed", "denied", "mixed", "none"):
        raise ValueError("decision must be one of allowed, denied, mixed, none")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    if not isinstance(tools_requested, list):
        raise ValueError("tools_requested must be an array of string")
    for t in tools_requested:
        if not isinstance(t, str):
            raise ValueError("tools_requested must be an array of string")
    if not isinstance(outcome, str):
        raise ValueError("outcome must be a string")
    ts = iso_now()
    tools_str = ",".join(tools_requested) if tools_requested else "none"
    outcome_str = outcome if outcome else "-"
    with file_lock(root):
        ensure_dirs(root)
        path = os.path.join(root, "EVOLUTION.md")
        s = "## " + ts + " | task: " + task + " | tools: " + tools_str + " | decision: " + decision + " | reason: " + reason + " | outcome: " + outcome_str + "\n"
        f = open(path, "a", encoding="utf-8")
        try:
            f.write(s)
        finally:
            f.close()
    return "EVOLUTION_RECORDED"


def do_pending_add(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "question" not in args:
        raise ValueError("missing required argument: question")
    if "category" not in args:
        raise ValueError("missing required argument: category")
    question = args["question"]
    category = args["category"]
    why = args.get("why", "")
    context = args.get("context", "")
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    if category not in ("tool-install", "system", "other"):
        raise ValueError("category must be one of tool-install, system, other")
    if not isinstance(why, str):
        raise ValueError("why must be a string")
    if not isinstance(context, str):
        raise ValueError("context must be a string")
    ts = iso_now()
    with file_lock(root):
        ensure_dirs(root)
        path = os.path.join(root, "PENDING.md")
        s = "### " + ts + " [" + category + "] " + question + "\nwhy: " + why + "\ncontext: " + context + "\n\n"
        f = open(path, "a", encoding="utf-8")
        try:
            f.write(s)
        finally:
            f.close()
        body = read_text(path)
        if body.startswith("# PENDING (empty)"):
            body = "# PENDING (open)" + body[len("# PENDING (empty)"):]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        count = sum(1 for ln in body.splitlines() if ln.startswith("###"))
    return s + "count: " + str(count)


def do_pending_get(root, args):
    with file_lock(root):
        path = os.path.join(root, "PENDING.md")
        if not os.path.exists(path):
            return "NO_PENDING"
        body = read_text(path)
        if not any(ln.startswith("###") for ln in body.splitlines()):
            return "NO_PENDING"
        return body


def do_pending_clear(root, args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "confirmed_summary" not in args:
        raise ValueError("missing required argument: confirmed_summary")
    confirmed_summary = args["confirmed_summary"]
    if not isinstance(confirmed_summary, str):
        raise ValueError("confirmed_summary must be a string")
    ts = iso_now()
    with file_lock(root):
        ensure_dirs(root)
        ppath = os.path.join(root, "PENDING.md")
        prev = ""
        has_content = False
        if os.path.exists(ppath):
            prev = read_text(ppath)
            if prev.strip() != "":
                has_content = True
        if has_content:
            arch = os.path.join(root, "archive", "PENDING-" + compact_now() + ".md")
            os.rename(ppath, arch)
            result = prev
        else:
            if not os.path.exists(ppath):
                result = "NONE"
            else:
                cur = read_text(ppath) if os.path.exists(ppath) else ""
                result = cur if cur.strip() != "" else "NONE"
                if result != "NONE":
                    pass
                else:
                    result = "NONE"
        with open(ppath, "w", encoding="utf-8") as f:
            f.write("# PENDING (empty)\n")
        log_path = os.path.join(root, "LOG.md")
        s = "## " + ts + " [pending] cleared after compiled confirmation: " + confirmed_summary + "\n\n"
        f2 = open(log_path, "a", encoding="utf-8")
        try:
            f2.write(s)
        finally:
            f2.close()
        return result


TOOLS = [
    {
        "name": "goal_set",
        "description": "Set the team goal, archiving any existing GOAL.md",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_ask": {"type": "string"},
                "expansion": {"type": "string"},
                "boundaries": {"type": "array", "items": {"type": "string"}},
                "approved": {"type": "string", "enum": ["pending", "yes", "no"], "default": "pending"},
            },
            "required": ["user_ask", "expansion", "boundaries"],
        },
    },
    {
        "name": "goal_get",
        "description": "Get the current GOAL.md content or NO_GOAL_SET",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "state_update",
        "description": "Overwrite STATE.md with current progress",
        "inputSchema": {
            "type": "object",
            "properties": {
                "current_step": {"type": "string"},
                "done": {"type": "array", "items": {"type": "string"}, "default": []},
                "next_step": {"type": "string", "default": ""},
                "evidence": {"type": "array", "items": {"type": "string"}, "default": []},
                "unverified": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["current_step"],
        },
    },
    {
        "name": "context_read",
        "description": "Read memory files (goal, state, decisions, log, user, evolution, pending, all)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["all"],
                }
            },
        },
    },
    {
        "name": "ledger_append",
        "description": "Append to LOG.md or DECISIONS.md",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["log", "decision"]},
                "text": {"type": "string"},
                "evidence": {"type": "string", "default": ""},
                "agent": {"type": "string", "default": ""},
            },
            "required": ["kind", "text"],
        },
    },
    {
        "name": "evolution_search",
        "description": "Search EVOLUTION.md entries by token overlap",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "evolution_record",
        "description": "Append a record to EVOLUTION.md",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "decision": {"type": "string", "enum": ["allowed", "denied", "mixed", "none"]},
                "reason": {"type": "string"},
                "tools_requested": {"type": "array", "items": {"type": "string"}, "default": []},
                "outcome": {"type": "string", "default": ""},
            },
            "required": ["task", "decision", "reason"],
        },
    },
    {
        "name": "pending_add",
        "description": "Append a question to PENDING.md and return entry count",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "category": {"type": "string", "enum": ["tool-install", "system", "other"]},
                "why": {"type": "string", "default": ""},
                "context": {"type": "string", "default": ""},
            },
            "required": ["question", "category"],
        },
    },
    {
        "name": "pending_get",
        "description": "Get PENDING.md content or NO_PENDING",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pending_clear",
        "description": "Archive PENDING.md, reset it, and log the clearance",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirmed_summary": {"type": "string"},
            },
            "required": ["confirmed_summary"],
        },
    },
]

HANDLERS = {
    "goal_set": do_goal_set,
    "goal_get": do_goal_get,
    "state_update": do_state_update,
    "context_read": do_context_read,
    "ledger_append": do_ledger_append,
    "evolution_search": do_evolution_search,
    "evolution_record": do_evolution_record,
    "pending_add": do_pending_add,
    "pending_get": do_pending_get,
    "pending_clear": do_pending_clear,
}


def handle_message(msg, root):
    method = msg.get("method") if isinstance(msg, dict) else None
    has_id = isinstance(msg, dict) and "id" in msg
    req_id = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(msg, dict) or not isinstance(method, str):
        if has_id:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "Invalid Request"}}
        return None
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        pv = params.get("protocolVersion") if isinstance(params, dict) else None
        if not isinstance(pv, str):
            pv = "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memory", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if not isinstance(params, dict):
            params = {}
        name = params.get("name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        try:
            if name not in HANDLERS:
                raise ValueError("unknown tool: %r" % (name,))
            eff_root = root
            try:
                meta = params.get("_meta") if isinstance(params, dict) else None
                sid = meta.get("opencode/session-id") if isinstance(meta, dict) else None
                if isinstance(sid, str) and sid and re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$", sid):
                    eff_root = os.path.join(root, "sessions", sid)
            except Exception:
                eff_root = root
            text = HANDLERS[name](eff_root, arguments)
            if not isinstance(text, str):
                text = str(text)
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            try:
                emsg = str(e) if str(e) else repr(e)
            except Exception:
                emsg = "error"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "Error: " + emsg}], "isError": True},
            }
    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root
    ensure_dirs(root)
    stdin = sys.stdin
    stdout = sys.stdout
    while True:
        line = stdin.readline()
        if line == "":
            break
        if line.strip() == "":
            continue
        try:
            msg = json.loads(line)
        except Exception:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
            continue
        try:
            resp = handle_message(msg, root)
        except Exception as e:
            try:
                rid = msg.get("id") if isinstance(msg, dict) and "id" in msg else None
            except Exception:
                rid = None
            if rid is None and not (isinstance(msg, dict) and "id" in msg):
                continue
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error: " + str(e)}}
        if resp is None:
            continue
        stdout.write(json.dumps(resp) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
