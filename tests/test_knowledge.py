"""The knowledge-base lookup on the clinic FAQ."""

import asyncio

import pytest
from pydantic import ValidationError

from app.calls.store import CallRecord
from app.schemas import CallConfig
from app.voice.knowledge import build_knowledge_tool, search


@pytest.mark.parametrize("query, section", [
    ("opening hours", "Opening hours"),
    ("is it open on Sunday", "Opening hours"),
    ("how much is a cleaning", "Prices"),
    ("root canal cost", "Prices"),
    ("where are you located", "Address and parking"),
    ("can I park my car", "Address and parking"),
    ("do you take UPI", "Insurance and payment"),
    ("what do you do", "About the clinic"),
    ("services", "About the clinic"),
])
def test_lookup_finds_the_right_section(query, section):
    assert search("clinic-faq", query)[0]["section"] == section


def test_lookup_returns_nothing_for_unrelated_questions():
    assert search("clinic-faq", "weather tomorrow") == []


def test_tool_logs_the_lookup_on_the_call():
    record = CallRecord(config=CallConfig.model_validate(
        {"systemPrompt": "x", "greeting": "hi", "direction": "inbound", "knowledgeBase": "clinic-faq"}))

    class Params:
        arguments = {"query": "parking"}
        async def result_callback(self, result, properties=None):
            self.result = result

    params = Params()
    asyncio.run(build_knowledge_tool(record, "clinic-faq").handler(params))
    assert params.result["found"] and "basement" in params.result["passages"][0]["text"]
    assert record.tools[-1]["name"] == "lookup_knowledge"


def test_unknown_knowledge_base_is_rejected():
    with pytest.raises(ValidationError):
        CallConfig.model_validate({"systemPrompt": "x", "greeting": "hi", "direction": "inbound",
                                   "knowledgeBase": "no-such-doc"})
