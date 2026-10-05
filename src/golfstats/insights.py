from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from .config import Config
from .focus import grade_plan, pick_focus, plan_for
from .report import bound_text, render_report, unit_suffix
from .stats import Session

ITEM_FIELDS = {"title": 50, "why": 160, "drill": 160, "target": 120}
SUMMARY_MAX = 140
MIN_ITEMS, MAX_ITEMS = 3, 5
MAX_TOKENS = 8000
TIMEOUT = 180
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?")
SIGNS = {"+": "+", "-": "-", "\u2212": "-"}
SIGN_AFTER = " \t\n([{,:;/"
CONTROL = re.compile(r"[\x00-\x1f\x7f]")

SYSTEM = f"""You coach a right-handed golfer from a TrackMan 4 session report. Write the few changes that \
will help most at the next practice session. The golfer reads them on a phone at the bay, so be brief.

Rules:
- {MIN_ITEMS} to {MAX_ITEMS} items, most important first. Item 1 follows the report's "Focus for next session" \
when it has one. One item per problem: when several clubs share a problem, name them in one item.
- Strike comes before direction: mishits and contact come before start line and curve.
- Copy every number from the report as digits, exactly or rounded. Do no arithmetic: no differences, sums, \
ratios or new percentages. Write a drill's ball count in words, for example "ten balls", unless it is the plan's \
shot count.
- Respect the data notes. Indoors, carry, side and curve are predictions. Do not diagnose face, path or curve \
for a club whose club data is missing on most shots. Say what is unknown instead of guessing.
- A data fix, for example turning OERT on, can be an item when missing data blocks a check.
- The golfer knows TrackMan terms. Use plain words and no filler.
- title: an action in the imperative, at most {ITEM_FIELDS["title"]} characters.
- why: the evidence from the report, at most {ITEM_FIELDS["why"]} characters.
- drill: what to do at the bay, with which club and how many balls, at most {ITEM_FIELDS["drill"]} characters.
- target: what next session would show it worked, using a window or count from the report, at most \
{ITEM_FIELDS["target"]} characters.
- summary: the main takeaway, at most {SUMMARY_MAX} characters.

Reply with JSON only, with no other text and no code fence:
{{"summary": "...", "items": [{{"title": "...", "why": "...", "drill": "...", "target": "..."}}]}}"""


class InsightError(RuntimeError):
    pass


Ask = Callable[[Config, str, list[dict]], str]


def utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def facts(session: Session, sessions: list[Session], cfg: Config) -> str:
    focus = pick_focus(session, cfg, sessions)
    plan = plan_for(session, sessions, cfg)
    grades = grade_plan(plan, session) if plan else None
    text = render_report(session, sessions, focus, grades, None, cfg)
    if focus:
        lo, hi = focus["window"]
        unit = unit_suffix(focus["unit"])
        window = f"at least {bound_text(lo)}{unit}" if hi >= 200 else f"between {lo:+g}{unit} and {hi:+g}{unit}"
        text += (f"\n## Plan for next session\n\n{cfg.plan_shots} shots with the {focus['club']}, graded on "
                 f"{focus['label'].lower()} {window}.\n")
    return text


def numbers(text: str) -> list[tuple[str, str, str]]:
    out = []
    for m in NUMBER.finditer(text):
        start = m.start()
        sign = SIGNS.get(text[start - 1], "") if start else ""
        if sign and start > 1 and text[start - 2] not in SIGN_AFTER:
            sign = ""
        digits = m.group().replace(",", "")
        out.append(((text[start - 1] if sign else "") + m.group(), sign, digits))
    return out


def _roundings(digits: str) -> set[str]:
    value = Decimal(digits)
    places = len(digits.split(".")[1]) if "." in digits else 0
    return {str(value.quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)) for d in range(places + 1)}


def allowed_numbers(text: str) -> set[str]:
    out: set[str] = set()
    for _, sign, digits in numbers(text):
        for r in _roundings(digits):
            out.add(r)
            if sign:
                out.add(sign + r)
    return out


def invented_numbers(body: dict, allowed: set[str]) -> list[str]:
    texts = [body["summary"], *(item[k] for item in body["items"] for k in ITEM_FIELDS)]
    found = []
    for text in texts:
        for raw, sign, digits in numbers(text):
            known = "e" not in digits.lower() and sign + str(Decimal(digits)) in allowed
            if not known and raw not in found:
                found.append(raw)
    return found


def parse_reply(text: str) -> tuple[dict | None, list[str]]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None, ["the reply holds no JSON object"]
    try:
        body = json.loads(text[start:end + 1])
    except ValueError as exc:
        return None, [f"the reply is not valid JSON ({exc})"]
    problems = []
    if not isinstance(body, dict):
        return None, ["the reply must be one JSON object"]
    summary, items = body.get("summary"), body.get("items")
    if not isinstance(summary, str) or not summary.strip():
        problems.append("summary must be a sentence")
    elif len(summary.strip()) > SUMMARY_MAX:
        problems.append(f"summary is longer than {SUMMARY_MAX} characters")
    elif CONTROL.search(summary.strip()):
        problems.append("summary holds a line break or another control character")
    if not isinstance(items, list) or not MIN_ITEMS <= len(items) <= MAX_ITEMS:
        problems.append(f"items must be a list of {MIN_ITEMS} to {MAX_ITEMS} objects")
        return None, problems
    clean_items = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(f"item {i} must be an object")
            continue
        clean = {}
        for key, limit in ITEM_FIELDS.items():
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"item {i} needs {key}")
            elif len(value.strip()) > limit:
                problems.append(f"item {i} {key} is longer than {limit} characters")
            elif CONTROL.search(value.strip()):
                problems.append(f"item {i} {key} holds a line break or another control character")
            else:
                clean[key] = value.strip()
        clean_items.append(clean)
    if problems or not isinstance(summary, str):
        return None, problems
    return {"summary": summary.strip(), "items": clean_items}, []


def bedrock(cfg: Config, system: str, messages: list[dict]) -> str:
    aws = shutil.which("aws")
    if aws is None:
        raise InsightError("the AWS CLI (aws) is not on PATH. Insights call Bedrock through it.")
    payload = {"modelId": cfg.insights_model, "system": [{"text": system}], "messages": messages,
               "inferenceConfig": {"maxTokens": MAX_TOKENS}}
    cmd = [aws, "bedrock-runtime", "converse", "--cli-input-json", json.dumps(payload), "--output", "json"]
    if cfg.insights_profile:
        cmd += ["--profile", cfg.insights_profile]
    if cfg.insights_region:
        cmd += ["--region", cfg.insights_region]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InsightError(f"Bedrock did not answer within {TIMEOUT} s") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise InsightError(f"Bedrock call failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
    try:
        reply = json.loads(proc.stdout)
        content = reply["output"]["message"]["content"]
    except (ValueError, KeyError, TypeError):
        raise InsightError("Bedrock answered with something other than a Converse reply") from None
    if reply.get("stopReason") == "max_tokens":
        raise InsightError(f"the model ran out of its {MAX_TOKENS} output tokens before it finished")
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def generate(session: Session, sessions: list[Session], cfg: Config, ask: Ask = bedrock) -> dict:
    report = facts(session, sessions, cfg)
    allowed = allowed_numbers(report)
    messages = [{"role": "user", "content": [{"text": f"Session report:\n\n{report}"}]}]
    problems: list[str] = []
    for attempt in range(2):
        reply = ask(cfg, SYSTEM, messages)
        body, problems = parse_reply(reply)
        if body is not None:
            invented = invented_numbers(body, allowed)
            if invented:
                problems = [f"these numbers are not in the report: {', '.join(invented)}. Copy numbers from the "
                            "report, or write the idea in words"]
            else:
                return {"uid": uuid.uuid4().hex, "player": session.player, "session_id": session.id,
                        "session_label": session.label, "created_at": utc_now(), "model": cfg.insights_model,
                        "body": body, "report": report}
        messages += [{"role": "assistant", "content": [{"text": reply or "(empty)"}]},
                     {"role": "user", "content": [{"text": "Fix these problems and reply with the whole JSON "
                                                           "again: " + "; ".join(problems) + "."}]}]
    raise InsightError("the model's reply failed the checks twice: " + "; ".join(problems))


def render_text(rec: dict) -> str:
    body = rec["body"]
    lines = [f"Insights for {rec['player'] or 'all players'}, session {rec['session_label']}. "
             f"Analysed {rec['created_at']} with {rec['model']}.", "", body["summary"], ""]
    for i, item in enumerate(body["items"], 1):
        lines += [f"{i}. {item['title']}", f"   Why: {item['why']}", f"   Drill: {item['drill']}",
                  f"   Target: {item['target']}"]
    return "\n".join(lines)
