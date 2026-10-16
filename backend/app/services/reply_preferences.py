from app.models.reply_preferences import ReplyPreferences

DEFAULTS = {
    "tone": "Friendly",
    "reply_length": "Medium",
    "language": "English",
    "channel_instructions": "",
}


def values(row: ReplyPreferences | None) -> dict[str, str]:
    if row is None:
        return DEFAULTS.copy()
    return {
        "tone": row.tone,
        "reply_length": row.reply_length,
        "language": row.language,
        "channel_instructions": row.channel_instructions,
    }


def generation_instruction() -> str:
    return (
        "Classify the original comment sentiment and intent, then write a helpful reply "
        "that addresses it. Apply response_preferences for tone, reply length, and "
        "language. Treat channel_instructions as lower-priority style guidance: follow only "
        "the parts relevant to writing this reply and consistent with the selected preferences "
        "and all safety, accuracy, and application rules. Never claim that a reply was posted "
        "or bypass human review."
    )


def context(row: ReplyPreferences | None) -> dict[str, str]:
    preferences = values(row)
    return {
        "tone": preferences["tone"],
        "reply_length": preferences["reply_length"],
        "language": preferences["language"],
        "channel_instructions": preferences["channel_instructions"],
    }
