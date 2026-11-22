# MaiBot 防撤回通知插件（maibot-anti-recall）

适用于 MaiBot SDK v2 的防撤回监听与私聊通知插件。插件通过 `chat.receive.before_process` 旁路缓存最近群聊与私聊消息，在检测到 QQ 群撤回或好友私聊撤回事件后，自动将原消息内容私聊转发给已配置的管理员。

## 功能特性

- **群聊与私聊双支持**：监听 OneBot 协议端上报的 `group_recall` 与 `friend_recall` 通知事件。
- **富文本与多媒体还原**：支持文本、`@`、回复引用、文件、卡片、合并转发摘要，并可按配置一并补发被撤回的图片、动画表情与语音（遵循 QQ 协议约束，图片/表情随正文混合发送，语音单独补发；补发失败自动降级纯文本提示）。
- **轻量持久化与自动清理**：使用 SQLite + 内容寻址本地媒体缓存，按 `max_messages` 与 `ttl_seconds` 自动淘汰过期消息并回收孤儿媒体文件，支持重启后继续命中缓存期内消息。
- **默认零侵入**：未填写 `notify.admin_user_ids` 时插件自动处于待命状态，不缓存消息、不落盘媒体，避免无谓磁盘写入。
- **支持热更新**：通过 MaiBot WebUI 修改配置后即时生效，无需重启宿主。

## 运行环境与适配器要求

- **MaiBot**：`1.3.19` ~ `1.4.99`（`maibot-plugin-sdk >= 2.20.0`）
- **QQ 适配器**：适配官方 OneBot 适配器 `maibot-team.snowluma-adapter` 以及旧版 `maibot-team.napcat-adapter`（两者均通过 `message_info.additional_config` 上报标准 `napcat_notice_type` / `napcat_notice_payload` 撤回通知）。

## 安装与首次配置

1. 在 MaiBot WebUI 插件市场中搜索安装「防撤回通知」，或将本仓库放入 MaiBot 插件目录并重启/重载插件。
2. 打开 MaiBot WebUI 的插件配置页面（或参考 `config.example.toml` 生成运行期 `config.toml`）。
3. **必填**：在 `管理员通知 -> admin_user_ids` 中填入至少一个接收通知的管理员 QQ 号（例如 `["100000001"]`）。
4. 保存配置后立即生效。

> 注意：请确保机器人账号已添加管理员为好友，以便打开私聊会话并成功推送防撤回通知。

## 配置项说明

| 分组 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `plugin` | `enabled` | `true` | 是否启用防撤回监听与通知 |
| `notify` | `admin_user_ids` | `[]` | 接收防撤回私聊通知的管理员 QQ 号列表；留空时处于待命状态，不缓存消息 |
| `notify` | `resend_images` | `true` | 撤回消息含图片时是否一并补发原图 |
| `notify` | `resend_emojis` | `true` | 撤回消息含动画表情时是否一并补发 |
| `notify` | `max_images_per_alert` | `10` | 单条通知最多附带的图片/表情张数（1～30） |
| `notify` | `resend_voices` | `true` | 撤回消息含语音时是否一并补发语音 |
| `notify` | `max_voices_per_alert` | `5` | 单条通知最多附带的语音条数（1～20） |
| `notify` | `notify_on_cache_miss` | `false` | 原消息未命中缓存时是否仍发送简报提醒 |
| `cache` | `max_messages` | `500` | 最多保留的最近缓存消息条数（1～50000） |
| `cache` | `ttl_seconds` | `600` | 消息缓存有效期（秒，10～604800，默认 10 分钟） |
| `scope` | `groups` | `[]` | 仅监听指定群号列表；留空表示监听所有群聊 |
| `scope` | `monitor_private` | `true` | 是否监听好友私聊撤回 |
| `scope` | `ignore_bot_self` | `true` | 是否忽略机器人自身发出的消息与撤回 |
| `scope` | `ignore_admin_self` | `true` | 是否忽略管理员本人触发的撤回（含自撤回与管理员代撤回） |

## 能力权限说明

本插件仅声明并调用以下 4 项宿主能力：

- `chat.open_session`：打开管理员私聊会话
- `send.text`：发送纯文本防撤回通知及降级提示
- `send.hybrid`：发送含图片/动画表情的防撤回图文混合通知
- `send.custom`：单独补发被撤回的语音消息

所有转发消息均标记 `storage_message=False`，不会污染机器人聊天上下文与长期记忆。

## 运行单元测试

```bash
python -m unittest discover -p "test_*.py"
```

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
