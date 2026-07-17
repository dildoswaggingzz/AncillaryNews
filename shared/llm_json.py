"""
Shared JSON-extraction helper for parsing Claude's text responses.

Both `shared/claim_extractor.py` (Claude Haiku) and
`shared/event_synthesizer.py` (Claude Opus) ask their respective system
prompts for "ONLY a JSON object", but a real (non-mocked) call confirmed
Claude Haiku routinely ignores that instruction and wraps its response in a
markdown code fence, e.g.:

    ```json
    {
      "summary": "...",
      "claims": [...]
    }
    ```

`json.loads` raises `json.JSONDecodeError` on that verbatim, so every real
extraction/synthesis call was silently failing (while 33+ mocked tests,
which never included fences, kept passing). `extract_json_object` centralizes
robust handling of the shapes Claude actually produces: bare JSON (the happy
path), fenced JSON (with or without a `json` language tag), and JSON with
incidental leading/trailing prose around it.
"""

import json
import re

# ```json ... ``` or ``` ... ``` — an optional language tag right after the
# opening fence, then everything up to the closing fence.
_FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)

# Belt-and-suspenders: the outermost {...} block, for cases with stray prose
# before/after the JSON that isn't fenced at all. Greedy so it spans nested
# braces within a single top-level object.
_BRACES_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(raw_text: str) -> dict | None:
    """
    Best-effort extraction of a JSON object from a Claude text response.

    Tries, in order:
    1. Parsing `raw_text` as-is (the documented/happy path).
    2. Stripping a markdown code fence (```` ```json ... ``` ```` or
       ```` ``` ... ``` ````) and parsing what's inside.
    3. Extracting the outermost `{...}` block from the raw text and parsing
       that, to tolerate incidental prose around the JSON.

    Returns the parsed `dict`, or `None` if no candidate parses as JSON —
    never raises, matching the "log and return None" contract both callers
    rely on.
    """
    if not isinstance(raw_text, str):
        return None

    candidates = [raw_text]

    fence_match = _FENCE_RE.search(raw_text)
    if fence_match:
        candidates.append(fence_match.group(1))

    braces_match = _BRACES_RE.search(raw_text)
    if braces_match:
        candidates.append(braces_match.group(0))

    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    return None
