"""Spelled letters recovered from recogniser text."""

import pytest

from app.voice.spelling import annotate, spelled_runs


@pytest.mark.parametrize("text, runs", [
    ("Yes, A and J", ["sanj"]),                 # the real call: S A N J
    ("No it S. A N J E Y.", ["sanjey"]),
    ("S a n j a y.", ["sanjay"]),
    ("S A N J A", ["sanja"]),
    ("es a en jay a why", ["sanjay"]),
    ("Yes, and you know what", []),             # ordinary speech: no plain letters
    ("Yes", []),
    ("I am Sanjay", []),
])
def test_spelled_runs(text, runs):
    assert spelled_runs(text) == runs


def test_annotation_shows_the_letters():
    assert annotate("Yes, A and J") == "Yes, A and J (spelled: S A N J)"
    assert annotate("Tomorrow at nine") == "Tomorrow at nine"


def test_whole_turn_is_annotated_when_letters_arrive_one_by_one():
    from app.voice.spelling import install_spelling

    class Aggregator:
        parts = ["My name is spelled S", "U", "N", "J"]   # NVIDIA: one final per letter

        def aggregation_string(self):
            return " ".join(self.parts)

    agg = Aggregator()
    install_spelling(agg)
    assert agg.aggregation_string() == "My name is spelled S U N J (spelled: S U N J)"
