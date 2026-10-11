from __future__ import annotations

from dataclasses import dataclass, field

from app.channels.base import ChannelAdapter, ChannelUnavailable, VoiceAdapter
from app.channels.fake import FakeAdapter, FakeVoiceAdapter
from app.models import Channel
from app.notifications import Notifier


@dataclass
class ChannelRegistry:
    adapters: dict[str, ChannelAdapter] = field(default_factory=dict)
    voice: VoiceAdapter | None = None
    notifier: Notifier | None = None

    def get(self, channel: str) -> ChannelAdapter:
        try:
            return self.adapters[channel]
        except KeyError as exc:
            raise ChannelUnavailable(f"no adapter configured for channel {channel!r}") from exc

    @classmethod
    def all_fake(cls) -> ChannelRegistry:
        return cls(
            adapters={c.value: FakeAdapter(channel=c.value) for c in Channel},
            voice=FakeVoiceAdapter(),
        )
