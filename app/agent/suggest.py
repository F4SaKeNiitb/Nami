"""Plain next-step suggestions after a site was connected, built from the site registry only.

Nothing here reads a page, an email or a model answer: the text comes from fixed wording and the registry's own endpoint
summaries, so a page cannot put words into the suggestion.
"""

from app.sites.loader import hints_for

_GOOGLE = ["summarise my latest unread emails", "find a file in my Drive and summarise it"]


def next_steps(site: str, *, limit: int = 3) -> list[str]:
    site = (site or "").lower().strip()
    if not site:
        return []
    hints = hints_for(site)
    out: list[str] = []
    if hints.site == "google.com":
        out += _GOOGLE
    endpoints = ((hints.api or {}).get("endpoints") or {}) if hints.api else {}
    for name, spec in endpoints.items():
        if spec.get("kind", "read") == "read" and spec.get("summary"):
            out.append(str(spec["summary"]).strip().rstrip(".").lower())
    generic = f"do another task on {hints.site}: I'll stay signed in, so you won't be asked again"
    return out[:max(limit - 1, 0)] + [generic] if limit > 0 else []      # the closing line always survives the cut


def suggestion_text(site: str) -> str:
    steps = next_steps(site)
    if not steps:
        return ""
    return "Next you could ask me to: " + "; ".join(steps) + "."
