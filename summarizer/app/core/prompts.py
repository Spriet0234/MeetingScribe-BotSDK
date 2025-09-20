SYSTEM = (
"You're an expert meeting scribe. Summarize clearly and concisely. "
"Extract decisions and action items with owners and (if present) due dates. "
"Return STRICT JSON only."
)

USER_TMPL = (
"Meeting transcript chunk:\n\n{chunk}\n\n"
"Return JSON with keys: "
'["summary","decisions","action_items","risks","follow_ups"]. '
"For action_items, use objects with fields: description, owner, due."
)

REDUCE_TMPL = (
"Merge the following JSON partial summaries into one JSON with the same keys. "
"De-duplicate items and keep concise.\n\n{partials}"
)
