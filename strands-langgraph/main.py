# notify_graph_agentic_with_plan_bedrock.py
from __future__ import annotations
import os, re, json, textwrap
from typing import TypedDict, Literal, List, Dict, Any, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, Field

# LangGraph
from langgraph.graph import StateGraph, END

# Strands Agents
from strands import Agent as StrandsAgent
from strands.models import BedrockModel
from strands import tool as s_tool
import boto3

# ------------------ CONFIG ------------------
STEP_MODES = {
    "plan": "agent",        # "deterministic" | "agent"
    "read": "agent",
    "check": "agent",
    "compose": "agent",
}
EMAIL_TO = os.getenv("EMAIL_TO", "user@example.com")
SEVERITY_THRESHOLD = 3
NOW = datetime.now()

# ------------------ Mock “databases” ------------------
INCIDENTS_DB = [
    {"id": "A-101", "title": "S3 5xx errors 2.1%", "severity": 2, "component": "storage",
     "details": "Transient spike us-west-2", "timestamp": (NOW - timedelta(hours=2)).isoformat()},
    {"id": "B-202", "title": "Payments error rate 12%", "severity": 4, "component": "billing",
     "details": "Timeouts to PSP", "timestamp": (NOW - timedelta(hours=1, minutes=20)).isoformat()},
    {"id": "D-404", "title": "Auth latency elevated 350ms", "severity": 3, "component": "auth",
     "details": "Redis cache hit drop", "timestamp": (NOW - timedelta(minutes=45)).isoformat()},
]
CHANGES_DB = [
    {"id": "C-303", "title": "Release r132", "severity": 1, "component": "api",
     "details": "Minor bugfix", "timestamp": (NOW - timedelta(hours=1)).isoformat()},
    {"id": "C-304", "title": "Payment gateway config tweak", "severity": 2, "component": "billing",
     "details": "Retry policy adjusted", "timestamp": (NOW - timedelta(minutes=30)).isoformat()},
]
METRICS_DB = {
    "billing_error_rate_5m": {"value": 0.11, "unit": "ratio", "window": "5m"},
    "auth_p95_latency_5m": {"value": 0.35, "unit": "seconds", "window": "5m"},
}

# ------------------ LLM plumbing via Strands (Bedrock Claude) ------------------
def _make_model():
    """Pick Anthropic on Bedrock (Claude 3.5 Sonnet v2) via a boto3 session."""
    session = boto3.Session(profile_name=os.getenv("AWS_PROFILE", "main"))
    bedrock_model = BedrockModel(
        model_id="us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        temperature=0.3,
        streaming=False,               # structured JSON is more reliable without streaming
        boto_session=session,
    )
    return bedrock_model

MODEL = _make_model()
LLM = StrandsAgent(model=MODEL)  # plain LLM client (no tools bound)

# ------------------ Robust JSON extraction & structured wrapper ------------------
def _extract_last_json_object(text: str) -> str:
    """
    Extract the LAST top-level JSON object from text.
    Ignores braces inside strings and handles escapes.
    """
    s = text.strip()
    end = s.rfind("}")
    if end == -1:
        raise ValueError("No closing brace found in model output.")
    depth = 0
    in_str = False
    esc = False
    start = None
    for i in range(end, -1, -1):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    start = i
                    break
    if start is None:
        # fallback to a broad DOTALL match
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            raise ValueError("No JSON object found in model output.")
        candidate = m.group(0)
    else:
        candidate = s[start:end+1]

    # Validate JSON
    json.loads(candidate)
    return candidate

def _structured(schema_cls, prompt: str):
    """
    Try Strands structured_output; if provider returns prose, parse last JSON object.
    """
    try:
        return LLM.structured_output(schema_cls, prompt)
    except Exception:
        res = LLM(prompt)
        txt = getattr(res, "output_text", str(res))
        js = _extract_last_json_object(txt)
        return schema_cls.model_validate_json(js)

# ------------------ Schemas ------------------
class Event(BaseModel):
    id: str
    title: str
    severity: int = Field(ge=0, le=5)
    component: str
    details: str
    timestamp: str

class ParseOutput(BaseModel):
    events: List[Event]

class InterestingDecision(BaseModel):
    id: str
    interesting: bool
    why: str

class ScreenOutput(BaseModel):
    decisions: List[InterestingDecision]

class EmailDraft(BaseModel):
    should_send: bool
    subject: str
    body: str

class Plan(BaseModel):
    run_steps: List[Literal["read","check","compose"]] = Field(
        description="Ordered subset of steps to run."
    )
    send_policy: Literal["auto","force_send","skip_send"] = "auto"
    rationale: Optional[str] = None

# ------------------ State ------------------
class NotifyState(TypedDict, total=False):
    plan: Plan
    queue: List[str]
    raw_db: Any
    records: List[Event]
    interesting: List[InterestingDecision]
    email: EmailDraft
    step_modes: Dict[str, str]

# ------------------ Deterministic Implementations ------------------
def read_deterministic() -> List[Event]:
    rows = INCIDENTS_DB + CHANGES_DB
    return [Event(**r) for r in rows]

def check_deterministic(records: List[Event]) -> List[InterestingDecision]:
    out: List[InterestingDecision] = []
    for ev in records:
        interesting = (ev.severity >= SEVERITY_THRESHOLD) or (
            "billing" in ev.component.lower() and ev.severity >= 2
        )
        why = "severity threshold" if ev.severity >= SEVERITY_THRESHOLD else (
            "billing component & sev>=2" if interesting else "below threshold"
        )
        out.append(InterestingDecision(id=ev.id, interesting=interesting, why=why))
    return out

def compose_deterministic(decisions: List[InterestingDecision], records: List[Event]) -> EmailDraft:
    ids = {d.id for d in decisions if d.interesting}
    items = [ev for ev in records if ev.id in ids]
    if not items:
        return EmailDraft(should_send=False, subject="No critical updates", body="No items exceeded thresholds.")
    bullet = "\n".join(f"- [{ev.severity}] {ev.title} ({ev.component}) @ {ev.timestamp}" for ev in items)
    return EmailDraft(should_send=True, subject=f"{len(items)} alerts detected", body=f"Interesting updates:\n\n{bullet}\n")

# ------------------ Agentic READ with tools ------------------
@s_tool
def db_get_incidents(severity_gte: int = 0, component: Optional[str] = None, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Query incidents with optional filters."""
    def ok(row):
        if row["severity"] < severity_gte: return False
        if component and row["component"].lower() != component.lower(): return False
        if since and row["timestamp"] <= since: return False
        return True
    return [r for r in INCIDENTS_DB if ok(r)]

@s_tool
def db_get_changes(component: Optional[str] = None, since: Optional[str] = None) -> List[Dict[str, Any]]:
    """Query change events with optional filters."""
    def ok(row):
        if component and row["component"].lower() != component.lower(): return False
        if since and row["timestamp"] <= since: return False
        return True
    return [r for r in CHANGES_DB if ok(r)]

@s_tool
def db_get_metrics(metric: str, window: str = "5m") -> Dict[str, Any]:
    """Get a metric snapshot by key."""
    for k, v in METRICS_DB.items():
        if k.startswith(metric) and v.get("window") == window:
            return {**v, "metric": k}
    return {"metric": metric, "window": window, "value": None, "unit": ""}

@s_tool
def db_search_component(q: str) -> List[str]:
    """Return component names that fuzzy-match q."""
    comps = {r["component"] for r in INCIDENTS_DB + CHANGES_DB}
    ql = q.lower()
    return [c for c in comps if ql in c.lower()]

def read_agentic_with_tools() -> List[Event]:
    agent = StrandsAgent(
        model=MODEL,
        tools=[db_get_incidents, db_get_changes, db_get_metrics, db_search_component],
    )
    prompt = f"""
    You are a data gathering agent for a notification service. Use tools to gather today's noteworthy
    incidents and changes. You may call tools multiple times with filters (severity_gte, component, since).
    Consider severity >= {SEVERITY_THRESHOLD} and anything billing/payments-related as likely noteworthy.

    At the END, output ONLY a JSON object for this schema (no prose):
      {{ "events": [{{"id": str, "title": str, "severity": int, "component": str, "details": str, "timestamp": str}}...] }}

    Current time: {NOW.isoformat()}.
    Prefer fresh items (last few hours). Avoid duplicates.
    """
    res = agent(prompt)
    txt = getattr(res, "output_text", str(res))
    js = _extract_last_json_object(txt)
    parsed: ParseOutput = ParseOutput.model_validate_json(js)
    return parsed.events

# ------------------ Agentic CHECK & COMPOSE ------------------
def check_agentic(records: List[Event]) -> List[InterestingDecision]:
    items = [ev.model_dump() for ev in records]
    examples = '{"decisions":[{"id":"X","interesting":true,"why":"payments and high severity"}]}'
    prompt = f"""
    You are a notification triage assistant. For each item, decide if it's interesting for on-call.
    Heuristics: severity >= {SEVERITY_THRESHOLD}; payments/billing high priority; repeated minor changes are not.
    Return ONLY JSON matching ScreenOutput like {examples}

    Items:
    {items}
    """
    out: ScreenOutput = _structured(ScreenOutput, prompt)
    return out.decisions

def compose_agentic(decisions: List[InterestingDecision], records: List[Event]) -> EmailDraft:
    by_id = {e.id: e for e in records}
    interesting = [d for d in decisions if d.interesting]
    prompt = f"""
    Draft a concise notification email with:
    - should_send: false if no interesting items, else true
    - subject: one line
    - body: short summary + bullets with [severity] title (component) @ timestamp. Max 120 words.
    User cares most about billing/payments and severity >= {SEVERITY_THRESHOLD}. TZ America/Los_Angeles.

    Decisions:
    {[d.model_dump() for d in decisions]}

    Records (only those marked interesting):
    {[by_id[d.id].model_dump() for d in interesting]}

    Return ONLY JSON matching:
    {EmailDraft.model_json_schema()}
    """
    return _structured(EmailDraft, prompt)

# ------------------ Planning ------------------
def plan_deterministic() -> Plan:
    return Plan(run_steps=["read","check","compose"], send_policy="auto", rationale="Default deterministic plan")

def plan_agentic() -> Plan:
    description = """
    You are the planner for a notification service with steps:
      - read: gather data (incidents/changes/metrics)
      - check: decide what's interesting
      - compose: draft the email
    Choose a subset and order of steps that makes sense *today*.
    Examples:
      - If there might be new data, include 'read'.
      - If you already have records, you might skip 'read'.
      - If nothing interesting is expected, you might skip 'compose'.
    send_policy:
      - "auto": send if composed draft has should_send=true
      - "force_send": send regardless (e.g., daily digest)
      - "skip_send": never send
    Return ONLY JSON for schema: {"run_steps":["read","check","compose"],"send_policy":"auto","rationale":"..."}
    """
    return _structured(Plan, description)

# ------------------ Email "Send" (mock) ------------------
def send_email_mock(to: str, draft: EmailDraft) -> None:
    print("\n=== SENDING EMAIL (MOCK) ===")
    print(f"To: {to}")
    print(f"Subject: {draft.subject}")
    print("Body:\n" + draft.body)
    print("=== END EMAIL ===\n")

# ------------------ LangGraph Nodes & Routing ------------------
class NotifyState(TypedDict, total=False):
    plan: Plan
    queue: List[str]
    raw_db: Any
    records: List[Event]
    interesting: List[InterestingDecision]
    email: EmailDraft
    step_modes: Dict[str, str]

def node_plan(state: NotifyState) -> NotifyState:
    mode = state["step_modes"]["plan"]
    plan = plan_agentic() if mode == "agent" else plan_deterministic()
    # sanitize plan
    allowed = {"read","check","compose"}
    run_steps = [s for s in plan.run_steps if s in allowed] or ["read","check","compose"]
    plan.run_steps = run_steps
    return {"plan": plan, "queue": list(run_steps)}

def route_dispatch(state: NotifyState) -> Literal["read","check","compose","finalize"]:
    q = state.get("queue", [])
    return q[0] if q else "finalize"

def node_dispatch(state: NotifyState) -> NotifyState:
    return {}

def _pop_queue(state: NotifyState) -> List[str]:
    q = state.get("queue", [])
    return q[1:] if q else []

def node_read(state: NotifyState) -> NotifyState:
    mode = state["step_modes"]["read"]
    if mode == "agent":
        records = read_agentic_with_tools()
        out = {"records": records, "raw_db": {"sources": ["incidents","changes","metrics"]}}
    else:
        records = read_deterministic()
        out = {"records": records, "raw_db": {"sources": ["structured_merged"]}}
    out["queue"] = _pop_queue(state)
    return out

def node_check(state: NotifyState) -> NotifyState:
    records = state.get("records", [])
    mode = state["step_modes"]["check"]
    decisions = check_agentic(records) if mode == "agent" else check_deterministic(records)
    return {"interesting": decisions, "queue": _pop_queue(state)}

def node_compose(state: NotifyState) -> NotifyState:
    decisions, records = state.get("interesting", []), state.get("records", [])
    mode = state["step_modes"]["compose"]
    draft = compose_agentic(decisions, records) if mode == "agent" else compose_deterministic(decisions, records)
    return {"email": draft, "queue": _pop_queue(state)}

def route_finalize(state: NotifyState) -> Literal["send","end"]:
    plan: Plan = state["plan"]
    policy = plan.send_policy
    draft: Optional[EmailDraft] = state.get("email")

    if policy == "skip_send":
        return "end"
    if draft is None:
        return "end"
    if policy == "force_send" or (policy == "auto" and draft.should_send):
        return "send"
    return "end"

def node_send(state: NotifyState) -> NotifyState:
    send_email_mock(EMAIL_TO, state["email"])
    return {}

# ------------------ Build & Run Graph ------------------
def build_graph():
    g = StateGraph(NotifyState)
    g.add_node("plan", node_plan)
    g.add_node("dispatch", node_dispatch)
    g.add_node("read", node_read)
    g.add_node("check", node_check)
    g.add_node("compose", node_compose)
    g.add_node("finalize", lambda s: {})  # no-op
    g.add_node("send", node_send)

    g.set_entry_point("plan")
    g.add_edge("plan", "dispatch")
    g.add_conditional_edges("dispatch", route_dispatch,
                            {"read": "read", "check": "check", "compose": "compose", "finalize": "finalize"})
    g.add_edge("read", "dispatch")
    g.add_edge("check", "dispatch")
    g.add_edge("compose", "dispatch")
    g.add_conditional_edges("finalize", route_finalize, {"send": "send", "end": END})
    g.add_edge("send", END)
    return g.compile()

def main():
    graph = build_graph()
    state: NotifyState = {"step_modes": STEP_MODES}
    out = graph.invoke(state)

    print("\n=== SUMMARY ===")
    print("Modes:", STEP_MODES)
    if "plan" in out:
        print("Plan:", out["plan"].model_dump())
    print(f"Read: {len(out.get('records', []))} record(s)")
    dec = out.get("interesting", [])
    print(f"Interesting: {len([d for d in dec if d.interesting])} / {len(dec)}")
    if "email" in out:
        print(f"Draft: should_send={out['email'].should_send}, subject={out['email'].subject!r}")

if __name__ == "__main__":
    main()
