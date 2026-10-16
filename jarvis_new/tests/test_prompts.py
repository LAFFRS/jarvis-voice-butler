from prompts import (
    BROWSER_INSTRUCTIONS,
    CODING_INSTRUCTIONS,
    JARVIS_INSTRUCTIONS,
)


def test_named_websites_are_opened_directly() -> None:
    assert "If the user names a website, service, or domain" in BROWSER_INSTRUCTIONS
    assert "Only use search_the_web when no website" in BROWSER_INSTRUCTIONS


def test_every_agent_keeps_the_butler_persona_and_hard_rule() -> None:
    for prompt in (JARVIS_INSTRUCTIONS, BROWSER_INSTRUCTIONS, CODING_INSTRUCTIONS):
        assert "sarcastic AI butler" in prompt
        assert '"At your service, Sir"' in prompt


def test_browser_rules_stay_out_of_main_and_coding_prompts() -> None:
    # Keeping each prompt to its own job keeps voice replies fast.
    assert "search_the_web" not in JARVIS_INSTRUCTIONS
    assert "search_the_web" not in CODING_INSTRUCTIONS


def test_coding_prompt_requires_consent_for_shell_access() -> None:
    assert "Only enable shell access after the user explicitly agrees" in (
        CODING_INSTRUCTIONS
    )
