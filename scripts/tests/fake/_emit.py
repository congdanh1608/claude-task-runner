import json
import sys

kind, session, text = sys.argv[1], sys.argv[2], sys.argv[3]
events = []
if kind == "codex":
    if session:
        events.append({"type": "thread.started", "thread_id": session})
    events.append({"type": "turn.started"})
    events.append({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    events.append({"type": "turn.completed", "usage": {}})
elif kind == "opencode":
    base = {"sessionID": session} if session else {}
    events.append(dict(base, type="step_start", part={"type": "step-start"}))
    events.append(dict(base, type="text", part={"type": "text", "text": text}))
    events.append(dict(base, type="step_finish", part={"type": "step-finish"}))
else:
    conv = {"conversation_id": session} if session else {}
    events.append({"event": "init", "init": dict(conv, model="m", tools=["x"])})
    events.append({"event": "step_update", "step_update": dict(conv, step_index=0, state="DONE", step_type="user_input",
                                                                text_delta="ignored prompt echo STATE: FAIL")})
    half = len(text) // 2
    for delta in (text[:half], text[half:] + "\n"):
        events.append({"event": "step_update", "step_update": dict(conv, step_index=1, state="ACTIVE",
                                                                    step_type="agent_response", text_delta=delta)})
    events.append({"event": "result", "result": dict(conv, status="SUCCESS", response=text + "\n")})
for event in events:
    print(json.dumps(event), flush=True)
