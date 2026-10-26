"""X 插件配置（WebUI 配置页由此自动生成）。"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

# 配置结构有增减时提升版本：MaiBot 只在版本升高时把新字段写回已有的 config.toml
SUPPORTED_CONFIG_VERSION = "1.0.0"
PLUGIN_VERSION = "0.1.0"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:56130/callback"


class PluginOptions(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(default=True, description="是否启用。", json_schema_extra={"label": "启用", "order": 0})
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="当前配置结构版本。",
        json_schema_extra={"label": "配置版本", "disabled": True, "hidden": True, "order": 99},
    )


class AppConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "X 开发者应用"
    __ui_order__: ClassVar[int] = 1

    client_id: str = Field(
        default="",
        description="X 开发者后台（console.x.com）里你的应用的 OAuth 2.0 Client ID。填好后在和她的私聊里发 /x_login。",
        json_schema_extra={"label": "Client ID", "input_type": "password", "order": 0},
    )
    client_secret: str = Field(
        default="",
        description="应用类型选的是「Web App / 机密客户端」才需要填；选「Native App / 公开客户端」就留空。",
        json_schema_extra={"label": "Client Secret（可不填）", "input_type": "password", "order": 1},
    )
    redirect_uri: str = Field(
        default=DEFAULT_REDIRECT_URI,
        description="回调地址，必须和 X 开发者后台里应用设置的 Callback URI 一字不差。一般不用改。",
        json_schema_extra={"label": "回调地址", "placeholder": DEFAULT_REDIRECT_URI, "order": 2},
    )


class BudgetConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "花费"
    __ui_order__: ClassVar[int] = 2

    monthly_usd: float = Field(
        default=10.0,
        ge=0.0,
        le=500.0,
        description="每个月最多花多少美元（每月 1 号重新算）。每次请求前按最贵的情况先算进去，不够就不请求；实际扣费以 X 开发者后台为准，建议那边也设同样的上限。",
        json_schema_extra={"label": "每月花费上限（美元）", "step": 1, "order": 0},
    )
    post_read_usd: float = Field(
        default=0.005,
        ge=0.0,
        le=1.0,
        description="读一条帖子的价格（美元）。X 改价时照开发者后台改这里。",
        json_schema_extra={"label": "读一条帖子", "step": 0.001, "order": 1},
    )
    user_read_usd: float = Field(
        default=0.010,
        ge=0.0,
        le=1.0,
        description="读一个账号资料的价格（美元）。帖子作者的名字也算在这里。",
        json_schema_extra={"label": "读一个账号", "step": 0.001, "order": 2},
    )
    action_usd: float = Field(
        default=0.015,
        ge=0.0,
        le=1.0,
        description="点赞、收藏、关注一次的价格（美元），按保守值估。",
        json_schema_extra={"label": "点赞 / 收藏 / 关注一次", "step": 0.001, "order": 3},
    )


class AccountConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "她的账号"
    __ui_order__: ClassVar[int] = 3

    like: bool = Field(default=True, description="允许她给喜欢的帖子点赞。", json_schema_extra={"label": "点赞", "order": 0})
    bookmark: bool = Field(default=True, description="允许她收藏帖子（X 的书签，别人看不到）。", json_schema_extra={"label": "收藏", "order": 1})
    follow: bool = Field(default=True, description="允许她关注喜欢的账号（她的时间线就是这些人发的）。", json_schema_extra={"label": "关注", "order": 2})
    requests_per_day: int = Field(default=150, ge=10, le=2000, description="每天最多发多少个请求（逛、看回复、各种操作合计）。", json_schema_extra={"label": "每天请求上限", "order": 3})
    likes_per_day: int = Field(default=20, ge=0, le=200, description="每天最多点赞几次。", json_schema_extra={"label": "每天点赞上限", "order": 4})
    bookmarks_per_day: int = Field(default=10, ge=0, le=100, description="每天最多收藏几条。", json_schema_extra={"label": "每天收藏上限", "order": 5})
    follows_per_day: int = Field(default=3, ge=0, le=50, description="每天最多关注几个账号。", json_schema_extra={"label": "每天关注上限", "order": 6})


class BrowseConfig(PluginConfigBase):
    __ui_label__: ClassVar[str] = "刷 X"
    __ui_order__: ClassVar[int] = 4

    list_count: int = Field(
        default=10,
        ge=5,
        le=30,
        description="刷时间线、看某人的帖子一次读几条（搜索和看回复 X 规定最少 10 条）。条数越多越贵。",
        json_schema_extra={"label": "每次几条", "order": 0},
    )
    images_per_call: int = Field(default=3, ge=0, le=6, description="一次最多让她看几张图（看到喜欢的可以收藏成表情包）。0 表示不看图。", json_schema_extra={"label": "每次几张图", "order": 1})
    include_replies: bool = Field(default=False, description="时间线里也显示回复别人的帖子（关掉更省钱、更干净）。", json_schema_extra={"label": "时间线里显示回复", "order": 2})
    blocked_keywords: list[str] = Field(default_factory=list, description="带这些词的帖子不给她看（照样计费，X 是按读到的条数收钱）。", json_schema_extra={"label": "屏蔽关键词", "order": 3})
    blocked_users: list[str] = Field(default_factory=list, description="这些账号（填 @ 后面的用户名）的帖子不给她看。", json_schema_extra={"label": "屏蔽账号", "order": 4})


class XBrowseConfig(PluginConfigBase):
    plugin: PluginOptions = Field(default_factory=PluginOptions)
    app: AppConfig = Field(default_factory=AppConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    account: AccountConfig = Field(default_factory=AccountConfig)
    browse: BrowseConfig = Field(default_factory=BrowseConfig)
