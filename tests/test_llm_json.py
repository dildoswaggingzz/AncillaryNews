import json

from shared.llm_json import extract_json_object


def test_extract_json_object_bare_json():
    payload = {"summary": "s", "claims": []}
    assert extract_json_object(json.dumps(payload)) == payload


def test_extract_json_object_fenced_with_json_language_tag():
    """Reproduces the real claude-haiku-4-5 response shape observed live."""
    payload = {
        "summary": "NBM confirms the Nordic mFRR MARI accession timeline.",
        "claims": [{"claim": "Something happened.", "claim_type": "fact"}],
    }
    raw = "```json\n" + json.dumps(payload, indent=2) + "\n```"

    assert extract_json_object(raw) == payload


def test_extract_json_object_fenced_without_language_tag():
    payload = {"summary": "s", "claims": []}
    raw = "```\n" + json.dumps(payload) + "\n```"

    assert extract_json_object(raw) == payload


def test_extract_json_object_with_surrounding_prose():
    payload = {"summary": "s", "claims": []}
    raw = f"Sure, here is the JSON:\n{json.dumps(payload)}\nLet me know if you need anything else."

    assert extract_json_object(raw) == payload


def test_extract_json_object_returns_none_on_unparseable_garbage():
    assert extract_json_object("not json at all") is None


def test_extract_json_object_returns_none_on_non_string_input():
    assert extract_json_object(None) is None


def test_extract_json_object_returns_none_when_json_is_not_an_object():
    assert extract_json_object("[1, 2, 3]") is None
