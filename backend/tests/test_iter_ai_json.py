"""Regressionstests für das tolerante JSON-Parsing der KI-Antworten."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.ai_json import parse_json_lenient


def test_valid_json_unchanged():
    assert parse_json_lenient('{"a": 1, "b": [1, 2]}') == {"a": 1, "b": [1, 2]}


def test_markdown_fence_and_prose():
    txt = 'Hier das Ergebnis:\n```json\n{"feedback": "ok", "suggestions": []}\n```\nViel Erfolg!'
    assert parse_json_lenient(txt)["feedback"] == "ok"


def test_trailing_comma_and_comments():
    txt = '{\n  // Kommentar\n  "a": 1,\n  "b": [1, 2,],\n}'
    assert parse_json_lenient(txt) == {"a": 1, "b": [1, 2]}


def test_truncated_object_is_salvaged():
    txt = ('{"feedback": "gut", "suggestions": ["mehr Filter", "engerer SL"], '
           '"rule_definition": {"timeframe": "1m", "indicators": {"ema": 200')
    data = parse_json_lenient(txt)
    assert data["feedback"] == "gut"
    assert data["suggestions"] == ["mehr Filter", "engerer SL"]


def test_truncated_inside_string_is_salvaged():
    txt = '{"feedback": "erster Teil", "improved_thesis": "hier bricht die Antwort ab'
    data = parse_json_lenient(txt)
    assert data["feedback"] == "erster Teil"


def test_braces_inside_strings_are_ignored():
    data = parse_json_lenient('{"note": "nutze {x} und [y]", "n": 2}')
    assert data["note"] == "nutze {x} und [y]" and data["n"] == 2


def test_empty_and_garbage_raise():
    for bad in ("", "   ", "kein json hier"):
        with pytest.raises(ValueError):
            parse_json_lenient(bad)
