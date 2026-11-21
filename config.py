"""Configuration declared through MaiBot SDK's native WebUI schema."""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用防撤回提醒；关闭后停止缓存新消息并暂停转发")
    config_version: str = Field(default="1.1.0", description="配置版本")


class NotifySection(PluginConfigBase):
    __ui_label__ = "管理员通知"
    __ui_icon__ = "bell"
    __ui_order__ = 1

    admin_user_ids: list[str] = Field(
        default_factory=list,
        description="接收防撤回私聊转发的管理员 QQ 号列表，支持填写多个；留空时插件待命，不缓存消息",
    )
    resend_images: bool = Field(
        default=True,
        description="检测到图片消息被撤回时，是否一并私聊补发原图",
    )
    resend_emojis: bool = Field(
        default=True,
        description="检测到动画表情/表情包被撤回时，是否一并私聊补发原图",
    )
    max_images_per_alert: int = Field(
        default=10,
        ge=1,
        le=30,
        description="单条撤回通知最多一并补发的图片/表情张数",
    )
    resend_voices: bool = Field(
        default=True,
        description="检测到语音消息被撤回时，是否一并私聊补发语音",
    )
    max_voices_per_alert: int = Field(
        default=5,
        ge=1,
        le=20,
        description="单条撤回通知最多一并补发的语音条数",
    )
    notify_on_cache_miss: bool = Field(
        default=False,
        description="若撤回的消息不在缓存中（如已过期、超出条数或重启前发送），是否仍发简报提醒",
    )


class CacheSection(PluginConfigBase):
    __ui_label__ = "消息缓存"
    __ui_icon__ = "database"
    __ui_order__ = 2

    max_messages: int = Field(
        default=500,
        ge=1,
        le=50000,
        description="最多缓存的最近消息条数，超出后按最早入库顺序淘汰",
    )
    ttl_seconds: int = Field(
        default=600,
        ge=10,
        le=604800,
        description="缓存消息的有效期（秒），超时自动清理；默认 600 秒（10 分钟）",
    )


class ScopeSection(PluginConfigBase):
    __ui_label__ = "监听范围"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    groups: list[str] = Field(
        default_factory=list,
        description="仅监听这些 QQ 群的消息与撤回；留空表示监听所有群聊",
    )
    monitor_private: bool = Field(
        default=True,
        description="是否同时监听好友私聊消息撤回",
    )
    ignore_bot_self: bool = Field(
        default=True,
        description="是否忽略机器人自身账号发出的消息及其撤回",
    )
    ignore_admin_self: bool = Field(
        default=False,
        description="是否忽略管理员本人撤回自己的消息",
    )


class AntiRecallConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    notify: NotifySection = Field(default_factory=NotifySection)
    cache: CacheSection = Field(default_factory=CacheSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)


__all__ = ["AntiRecallConfig"]
