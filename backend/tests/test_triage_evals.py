import os

import pytest

from app.services import llm


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("RUN_LIVE_EVALS"), reason="Opt-in with RUN_LIVE_EVALS=1")
async def test_triage_realistic_evaluations():
    api_key = os.environ.get("SECONDARY_LLM_API_KEY")
    if not api_key:
        from app.config import settings
        api_key = getattr(settings, 'secondary_llm_api_key', None)
        if api_key:
            api_key = api_key.get_secret_value()
    
    if not api_key or 'test' in api_key.lower():
        pytest.skip("Skipping realistic eval: Real SECONDARY_LLM_API_KEY required.")

    from app.config import settings

    provider = llm.provider("secondary")
    
    instruction = (
        "Assess if the viewer's comment is relevant to the video discussion. "
        "on_topic means clearly relevant; generic_engagement means generic praise like 'nice video'; "
        "off_topic means unrelated to the video context; spam_or_suspicious means spam/scam; "
        "uncertain means insufficient evidence to decide. Give a short reason and a confidence score."
    )
    
    # Eval 1: Explicit C++ context vs On-Topic comment
    data_cpp_ontopic = {
        "comment": "Why use std::vector over std::list here?",
        "video_title": "C++ Data Structures Tutorial",
        "video_description": "Explaining vectors and lists in C++",
        "channel_title": "Code Channel"
    }
    result_ontopic = await provider.complete(llm.Triage, instruction, data_cpp_ontopic)
    assert result_ontopic.category == "on_topic"
    assert result_ontopic.confidence >= 0.8
    
    # Eval 2: Explicit C++ context vs Unrelated comment (bananas)
    data_cpp_unrelated = {
        "comment": "I love eating bananas in the morning!",
        "video_title": "C++ Data Structures Tutorial",
        "video_description": "Explaining vectors and lists in C++",
        "channel_title": "Code Channel"
    }
    result_unrelated = await provider.complete(llm.Triage, instruction, data_cpp_unrelated)
    assert result_unrelated.category == "off_topic"
    
    # Eval 3: Generic Engagement
    data_generic = {
        "comment": "Awesome video, thanks!",
        "video_title": "C++ Data Structures Tutorial",
        "video_description": "Explaining vectors and lists in C++",
        "channel_title": "Code Channel"
    }
    result_generic = await provider.complete(llm.Triage, instruction, data_generic)
    assert result_generic.category == "generic_engagement"

    # Eval 4: Spam
    data_spam = {
        "comment": "Win free crypto at scam-link.com",
        "video_title": "C++ Data Structures Tutorial",
        "video_description": "Explaining vectors and lists in C++",
        "channel_title": "Code Channel"
    }
    result_spam = await provider.complete(llm.Triage, instruction, data_spam)
    assert result_spam.category == "spam_or_suspicious"

