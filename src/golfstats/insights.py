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

ITEM_FIELDS = {"title": 50, "why": 160, "target": 120}
DRILL_FIELDS = {"name": 40, "setup": 160, "reps": 120, "pass": 120}
SUMMARY_MAX = 140
BEFORE_MAX = 160
MIN_ITEMS, MAX_ITEMS = 3, 5
MAX_TOKENS = 16000
TIMEOUT = 300
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?")
SIGNS = {"+": "+", "-": "-", "\u2212": "-"}
SIGN_AFTER = " \t\n([{,:;/"
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
NUMBER_WORD = re.compile(r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
                         r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
                         r"eighty|ninety|hundred|thousand|dozen|half|halves|quarters?|twice|thrice|double|triple|"
                         r"(?:third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)s?(?=\s+of\b))\b", re.IGNORECASE)

SYSTEM = f"""You coach a right-handed golfer from a TrackMan 4 session report. Write the few changes that \
will help most at the next practice session, each with a real drill. The golfer reads this on a phone at the \
bay, so be brief.

Rules:
- {MIN_ITEMS} to {MAX_ITEMS} items, most important first. Item 1 follows the report's "Focus for next session" \
when it has one. One item per problem: when several clubs share a problem, name them in one item.
- Strike comes before direction: mishits and contact come before start line and curve.
- In summary, before, title, why and target, every number is copied from the report as digits, exactly or \
rounded. Do no arithmetic: no differences, sums, ratios or new percentages. Never write a number as a word there.
- Only the drill prescribes numbers of its own, such as sets, balls and distances. Write those in words: \
"three sets of five", "two feet ahead". Never state a measurement of the golfer inside the drill.
- Respect the data notes. Indoors, carry, side and curve are predictions. Do not diagnose face, path or curve \
for a club whose club data is missing on most shots. Say what is unknown instead of guessing.
- Missing data is not an item. When a setting or the setup blocks a check, for example OERT is off, say how to \
fix it in "before". Otherwise leave "before" empty.
- The golfer knows TrackMan terms. Use plain words and no filler.

Drills:
- A drill is a known practice drill with a physical setup or instant feedback, aimed at the cause the data \
points to. Examples: a gate of two tees or alignment sticks a few feet ahead on the start line; tees just \
outside the heel and the toe; a headcover just outside the ball against heel strikes; foot powder spray or \
impact tape on the face; a towel behind the ball against fat strikes; an alignment stick on the ground along \
the club path; a pause at the top; feet-together swings; a split-hand grip.
- "Hit balls and focus on it", "try to", "feel" or "slow down" on its own is not a drill.
- When the data cannot show the cause, pick a drill that shows it, for example spray on the face to see where \
the mishits strike.
- drill.name: the drill's usual name, at most {DRILL_FIELDS["name"]} characters.
- drill.setup: what to place where before the first ball, at most {DRILL_FIELDS["setup"]} characters.
- drill.reps: the club and the sets and balls, at most {DRILL_FIELDS["reps"]} characters. For example: \
"Driver, three sets of five."
- drill.pass: what you see at the bay that says you can move on, at most {DRILL_FIELDS["pass"]} characters. \
For example: "Four of five start through the gate."

Fields:
- title: an action in the imperative, at most {ITEM_FIELDS["title"]} characters.
- why: the evidence from the report, at most {ITEM_FIELDS["why"]} characters.
- target: what next session would show it worked, using a window or count from the report, at most \
{ITEM_FIELDS["target"]} characters.
- summary: the main takeaway, at most {SUMMARY_MAX} characters.
- before: a setup fix to make before the first ball, at most {BEFORE_MAX} characters, or "".

Reply with JSON only, with no other text and no code fence:
{{"summary": "...", "before": "...", "items": [{{"title": "...", "why": "...", \
"drill": {{"name": "...", "setup": "...", "reps": "...", "pass": "..."}}, "target": "..."}}]}}"""


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


def body_texts(body: dict) -> list[str]:
    texts = [body["summary"], body.get("before", "")]
    for item in body["items"]:
        texts += [item[k] for k in ITEM_FIELDS] + [item["drill"][k] for k in DRILL_FIELDS]
    return texts


def number_words(body: dict) -> list[str]:
    texts = [body["summary"], body.get("before", "")]
    for item in body["items"]:
        texts += [item[k] for k in ITEM_FIELDS]
    found: list[str] = []
    for text in texts:
        for word in NUMBER_WORD.findall(text):
            if word.lower() not in found:
                found.append(word.lower())
    return found


def invented_numbers(body: dict, allowed: set[str]) -> list[str]:
    texts = body_texts(body)
    found = []
    for text in texts:
        for raw, sign, digits in numbers(text):
            known = "e" not in digits.lower() and sign + str(Decimal(digits)) in allowed
            if not known and raw not in found:
                found.append(raw)
    return found


def _text(value, name: str, limit: int, problems: list[str], required: bool = True) -> str | None:
    if not isinstance(value, str) or (required and not value.strip()):
        problems.append(f"{name} must be {'text' if not required else 'a sentence'}")
    elif len(value.strip()) > limit:
        problems.append(f"{name} is longer than {limit} characters")
    elif CONTROL.search(value.strip()):
        problems.append(f"{name} holds a line break or another control character")
    else:
        return value.strip()
    return None


def parse_reply(text: str) -> tuple[dict | None, list[str]]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None, ["the reply holds no JSON object"]
    try:
        body = json.loads(text[start:end + 1])
    except ValueError as exc:
        return None, [f"the reply is not valid JSON ({exc})"]
    problems: list[str] = []
    if not isinstance(body, dict):
        return None, ["the reply must be one JSON object"]
    summary = _text(body.get("summary"), "summary", SUMMARY_MAX, problems)
    before = _text(body.get("before", ""), "before", BEFORE_MAX, problems, required=False)
    items = body.get("items")
    if not isinstance(items, list) or not MIN_ITEMS <= len(items) <= MAX_ITEMS:
        problems.append(f"items must be a list of {MIN_ITEMS} to {MAX_ITEMS} objects")
        return None, problems
    clean_items = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            problems.append(f"item {i} must be an object")
            continue
        clean = {key: _text(item.get(key), f"item {i} {key}", limit, problems) for key, limit in ITEM_FIELDS.items()}
        drill = item.get("drill")
        if not isinstance(drill, dict):
            problems.append(f"item {i} drill must be an object with name, setup, reps and pass")
            continue
        clean["drill"] = {key: _text(drill.get(key), f"item {i} drill.{key}", limit, problems)
                          for key, limit in DRILL_FIELDS.items()}
        clean_items.append(clean)
    if problems:
        return None, problems
    out = {"summary": summary, "items": clean_items}
    if before:
        out["before"] = before
    return out, []


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
    for _ in range(2):
        reply = ask(cfg, SYSTEM, messages)
        body, problems = parse_reply(reply)
        if body is not None:
            invented, words = invented_numbers(body, allowed), number_words(body)
            problems = []
            if invented:
                problems.append(f"these numbers are not in the report: {', '.join(invented)}. Copy numbers from "
                                "the report, or write the idea without a number")
            if words:
                problems.append(f"summary, before, title, why and target hold number words: {', '.join(words)}. "
                                "Write report numbers as digits there, and keep number words in the drill")
            if not problems:
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
    if body.get("before"):
        lines += [f"Before you hit: {body['before']}", ""]
    for i, item in enumerate(body["items"], 1):
        drill = item["drill"]
        lines += [f"{i}. {item['title']}", f"   Why: {item['why']}", f"   Drill: {drill['name']}",
                  f"     Set up: {drill['setup']}", f"     Do: {drill['reps']}", f"     Done when: {drill['pass']}",
                  f"   Target: {item['target']}"]
    return "\n".join(lines)
