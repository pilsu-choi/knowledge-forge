from knowledge_forge.context.base import ContextReconstructor, NoopReconstructor
from knowledge_forge.context.gmail import GmailThreadReconstructor
from knowledge_forge.context.slack import SlackConversationReconstructor
from knowledge_forge.models.source import SourceType

_BY_SOURCE_TYPE: dict[SourceType, type[ContextReconstructor]] = {
    SourceType.SLACK: SlackConversationReconstructor,
    SourceType.GMAIL: GmailThreadReconstructor,
}


def default_reconstructor_for(source_type: SourceType) -> ContextReconstructor:
    """File/Google Drive/Notion items are already independent semantic units
    (see wiki/roadmap.md), so they get an identity NoopReconstructor."""
    reconstructor_cls = _BY_SOURCE_TYPE.get(source_type, NoopReconstructor)
    return reconstructor_cls()
