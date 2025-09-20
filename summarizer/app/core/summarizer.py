import json
from app.core.openai_client import OpenAICompatClient
from app.core.prompts import SYSTEM, USER_TMPL, REDUCE_TMPL
from app.core.chunking import chunk_text

def force_json(s: str) -> dict:
    import re, json
    m = re.search(r"\{[\s\S]*\}", s)
    if not m: return {"summary":"","decisions":[],"action_items":[],"risks":[],"follow_ups":[]}
    try: return json.loads(m.group(0))
    except: 
        cleaned = re.sub(r",(\s*[}\]])", r"\1", m.group(0))
        try: return json.loads(cleaned)
        except: return {"summary": s.strip(), "decisions":[],"action_items":[],"risks":[],"follow_ups":[]}

async def summarize_text(text: str, model: str | None = None) -> dict:
    client = OpenAICompatClient(model=model)
    chunks = chunk_text(text)
    partials = []
    for c in chunks:
        out = await client.chat(SYSTEM, USER_TMPL.format(chunk=c))
        partials.append(force_json(out))
    if len(partials) == 1:
        return partials[0]
    combined = await client.chat(SYSTEM, REDUCE_TMPL.format(partials=json.dumps(partials, ensure_ascii=False)))
    return force_json(combined)

def to_markdown(obj: dict) -> str:
    md = ["# Meeting Summary\n"]
    if obj.get("summary"): md += ["## Summary\n", obj["summary"], ""]
    if obj.get("decisions"):
        md += ["## Decisions"]; md += [f"- {d}" for d in obj["decisions"]]; md.append("")
    if obj.get("action_items"):
        md += ["## Action Items"]
        for a in obj["action_items"]:
            owner = a.get("owner",""); due = a.get("due","")
            md.append(f"- {owner+': ' if owner else ''}{a.get('description','')}{f' (due: {due})' if due else ''}")
        md.append("")
    if obj.get("risks"):
        md += ["## Risks"]; md += [f"- {r}" for r in obj["risks"]]; md.append("")
    if obj.get("follow_ups"):
        md += ["## Follow-ups"]; md += [f"- {f}" for f in obj["follow_ups"]]; md.append("")
    return "\n".join(md)
