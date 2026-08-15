"""Regressionstests für den Lektions-Bugfix des KI Traders.

Bug: pro Lernlauf wurde die komplette Lektionsliste durch die LLM-Antwort
ersetzt. Ein Modell liefert typischerweise 3-5 Lektionen -> es blieben trotz
`max_lessons = 50` dauerhaft nur ~5 gespeichert.
"""
from services.ai_learning import merge_lessons


def L(title, weight=2, updated_at="2026-01-01"):
    return {"title": title, "detail": f"detail {title}", "weight": weight,
            "updated_at": updated_at}


def test_old_lessons_survive_a_run_with_few_new_lessons():
    old = [L(f"Lektion {i}") for i in range(1, 6)]
    new = [L("Neu A"), L("Neu B")]
    merged = merge_lessons(old, new, [], 50)
    titles = [m["title"] for m in merged]
    assert len(merged) == 7
    assert "Neu A" in titles and "Lektion 1" in titles


def test_repeated_runs_accumulate_up_to_limit():
    lessons = []
    for run in range(20):
        fresh = [L(f"R{run}-a"), L(f"R{run}-b")]
        lessons = merge_lessons(lessons, fresh, [], 50)
    assert len(lessons) == 40  # 20 Läufe x 2 -> kein Verlust mehr


def test_limit_is_respected():
    old = [L(f"alt {i}", weight=1) for i in range(60)]
    merged = merge_lessons(old, [L("neu", weight=3)], [], 10)
    assert len(merged) == 10
    assert merged[0]["title"] == "neu"  # stärkstes Gewicht zuerst


def test_duplicate_titles_are_updated_not_doubled():
    old = [L("Gleiche Lektion", weight=1)]
    new = [{"title": "gleiche lektion", "detail": "geschärft", "weight": 3}]
    merged = merge_lessons(old, new, [], 50)
    assert len(merged) == 1
    assert merged[0]["detail"] == "geschärft"


def test_removed_lessons_are_dropped():
    old = [L("Behalten"), L("Verwerfen")]
    merged = merge_lessons(old, [], ["verwerfen"], 50)
    assert [m["title"] for m in merged] == ["Behalten"]


def test_entries_without_title_are_ignored():
    merged = merge_lessons([], [{"detail": "kein titel"}, L("ok")], [], 50)
    assert [m["title"] for m in merged] == ["ok"]
