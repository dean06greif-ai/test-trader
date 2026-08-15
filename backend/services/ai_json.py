"""Tolerantes Einlesen von JSON-Antworten der KI-Modelle.

Modelle liefern gelegentlich *fast* gültiges JSON: Markdown-Zäune, Kommentare,
überzählige Kommas oder – bei langen Antworten – ein abgeschnittenes Ende. Genau
das hat einzelne KI-Aufrufe komplett scheitern lassen.

`parse_json_lenient` versucht deshalb in dieser Reihenfolge:
  1. strikt (unverändertes Verhalten für gültiges JSON),
  2. offensichtliche Störungen entfernen (Zäune, Kommentare, Trailing-Kommas),
  3. abgeschnittene Antworten retten: bis zum letzten vollständigen Wert
     zurückschneiden und offene Klammern schliessen.

Alle Funktionen sind rein und damit direkt testbar.
"""
import json
import re
from typing import Dict

_FENCE = re.compile(r"```(?:json)?", re.IGNORECASE)
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _slice_object(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        raise ValueError("Keine JSON-Antwort der KI")
    return text[start:end + 1] if end > start else text[start:]


def _clean(text: str) -> str:
    text = _FENCE.sub("", text)
    text = _BLOCK_COMMENT.sub("", text)
    text = _LINE_COMMENT.sub("", text)
    return _TRAILING_COMMA.sub(r"\1", text).strip()


def _salvage(text: str) -> str:
    """Abgeschnittenes JSON bis zum letzten vollständigen Wert zurückschneiden
    und offene Klammern/Anführungszeichen schliessen."""
    stack = []
    in_str = False
    escape = False
    safe_cut = None          # Position NACH dem letzten vollständigen Wert
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
                if stack and stack[-1] == "[":
                    safe_cut = i + 1
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
            safe_cut = i + 1
        elif ch == ",":
            safe_cut = i          # Komma selbst wird abgeschnitten
    if safe_cut is None:
        raise ValueError("JSON-Antwort der KI unbrauchbar")
    head = text[:safe_cut].rstrip().rstrip(",")
    # Klammer-Stack für den gekürzten Teil neu bestimmen und schliessen
    stack, in_str, escape = [], False, False
    for ch in head:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        head += '"'
    return head + "".join("}" if b == "{" else "]" for b in reversed(stack))


def parse_json_lenient(text: str) -> Dict:
    """JSON aus einer Modell-Antwort lesen – strikt, sonst repariert."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Leere KI-Antwort")
    body = _slice_object(_FENCE.sub("", text).strip())
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    cleaned = _slice_object(_clean(text))
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    data = json.loads(_salvage(cleaned))
    if not isinstance(data, dict):
        raise ValueError("JSON-Antwort der KI ist kein Objekt")
    return data
