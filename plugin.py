"""MaiBot X 插件：让她用自己的 X（推特）账号刷帖子。

走 X 官方接口（API v2，按量付费）：主人在 X 开发者后台建应用、填 Client ID，发 /x_login 授权她的账号。
她可以刷自己关注的人的时间线、搜帖子、看某个人的帖子、看一条帖子下面的回复（带图），
给喜欢的点赞、收藏，关注喜欢的账号；看到的图可以收藏进 MaiBot 的表情包。
刷不刷、刷什么、做不做，全由她用工具自己决定；插件只提供眼睛和手，不替她调模型、不发帖、不回复、不私信。
每月花费有上限（默认 10 美元），请求前按最贵的情况先算进去，不够就不请求。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

import asyncio
import base64
import io

import aiohttp
from maibot_sdk import Command, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config import PLUGIN_VERSION, XBrowseConfig
from .core.account import ACTION_NAMES, Account, AccountPaused, Limits, Prices, QuotaExceeded
from .core.xapi import AppSettings, Pending, Post, XClient, XError, post_id_of, username_of

BROWSE_TOOL = "x_browse"
REPLIES_TOOL = "x_replies"
ACTION_TOOL = "x_action"
EMOJI_TOOL = "x_save_emoji"
BROWSE_DESCRIPTION = (
    "用你自己的 X（推特）账号刷帖子：source 填「时间线」（你关注的人最近发的，关注谁由你自己决定）、"
    "「搜索」（query 填关键词，只搜最近 7 天）、「某人」（query 填 @用户名 或主页链接）或「帖子」（query 填帖子链接，对方发来 x.com / twitter.com 链接时用这个）。"
    "刷时间线想接着往下看就把 more 填 true。帖子里的图会一起给你看（标成「图1」「图2」）。"
    "每次都要花真钱（按读到的条数算），想刷再刷，刷到没意思就停。"
)
REPLIES_DESCRIPTION = (
    "看一条帖子下面大家的回复（只能找到最近 7 天的）。看看大家怎么聊、有什么梗；"
    "看到有意思的说法可以用 learn_expression 学下来，没见过的梗弄懂了可以用 learn_jargon 记下，喜欢的图可以用 x_save_emoji 收藏成表情包。"
)
ACTION_DESCRIPTION = (
    "用你自己的 X 账号做点什么：action 填「点赞」「收藏」或「关注」。post 填帖子 ID 或链接；关注时填帖子（关注它的作者）或 @用户名。"
    "真喜欢再做；你关注的人决定了你的时间线能刷到什么。每天有次数上限，做过的不会重复做。"
)
EMOJI_DESCRIPTION = "把刚才在 X 上看到的一张图收藏成你的表情包（image 填「图1」这样的编号）。只收藏你真觉得有意思、以后想拿来用的。"
SOURCES = {"时间线": "timeline", "关注": "timeline", "推荐": "timeline", "搜索": "search", "某人": "user", "用户": "user", "帖子": "post", "链接": "post"}
ACTION_KEYS = {"点赞": "like", "喜欢": "like", "收藏": "bookmark", "书签": "bookmark", "关注": "follow", "关注作者": "follow"}
TEXT_LIMIT = 400
# 图交给她看时缩到多大；一次最多编号几张图、最近一共记住几张（要大于前者）
IMAGE_MAX_SIDE = 512
IMAGE_PER_CALL = 20
IMAGE_KEEP = 60
POST_KEEP = 300


class XBrowsePlugin(MaiBotPlugin):
    config_model: ClassVar[type] = XBrowseConfig

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._account: Optional[Account] = None
        self._client: Optional[XClient] = None
        self._pending: Optional[Pending] = None
        self._next_token = ""  # 时间线「接着往下刷」用
        self._posts: Dict[str, Post] = {}  # 最近读过的帖子（关注作者时不用再花钱查一次）
        self._images: Dict[str, str] = {}  # 「图N」→ 图片地址（编号一直往上加，不会串）
        self._image_seq = 0
        self._action_lock = asyncio.Lock()  # 账号操作一个一个来：配额、去重、间隔才不会被同时绕过
        self._last_error = ""

    # ── 生命周期 ──────────────────────────────────

    async def on_load(self) -> None:
        self._account = Account(Path(self.ctx.paths.data_dir) / "account", self._limits(), self._prices())
        await self._build()
        self.ctx.logger.info("X 插件已加载（v%s）", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        await self._close()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version
        if self._account is not None:
            self._account.limits = self._limits()
            self._account.prices = self._prices()
        await self._close()
        await self._build()
        self.ctx.logger.info("X 插件配置已生效")

    async def _build(self) -> None:
        # trust_env：沿用 MaiBot 容器里设置的 HTTP(S)_PROXY
        self._session = aiohttp.ClientSession(trust_env=True, cookie_jar=aiohttp.DummyCookieJar())
        app = self.config.app
        assert self._account is not None
        self._client = XClient(
            self._session,
            self._account,
            AppSettings(client_id=app.client_id.strip(), client_secret=app.client_secret.strip(), redirect_uri=app.redirect_uri.strip()),
            logger=self.ctx.logger,
        )

    async def _close(self) -> None:
        self._client = None
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def _limits(self) -> Limits:
        cfg = self.config.account
        limits = Limits(requests_per_day=cfg.requests_per_day)
        limits.per_day.update(like=cfg.likes_per_day, bookmark=cfg.bookmarks_per_day, follow=cfg.follows_per_day)
        return limits

    def _prices(self) -> Prices:
        cfg = self.config.budget
        return Prices(monthly=cfg.monthly_usd, post_read=cfg.post_read_usd, user_read=cfg.user_read_usd, action=cfg.action_usd)

    def _usable(self) -> Optional[str]:
        if not self.config.plugin.enabled or self._client is None:
            return "X 的功能现在关着"
        if not self._client.account.logged_in:
            return "还没授权你自己的 X 账号（等主人发 /x_login）"
        return None

    # ── 刷帖子 ────────────────────────────────────

    @Tool(
        BROWSE_TOOL,
        description=BROWSE_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="source", param_type=ToolParamType.STRING, description="时间线、搜索、某人 或 帖子"),
            ToolParameterInfo(name="query", param_type=ToolParamType.STRING, description="搜索的关键词、@用户名 或帖子链接；刷时间线不填", required=False),
            ToolParameterInfo(name="more", param_type=ToolParamType.BOOLEAN, description="刷时间线时接着上次往下看", required=False),
        ],
        chat_scope="private",
    )
    async def tool_browse(self, source: str = "时间线", query: str = "", more: Any = False, **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        client = self._client
        assert client is not None
        cfg = self.config.browse
        where = SOURCES.get(str(source or "时间线").strip(), "timeline")
        query = str(query or "").strip()
        try:
            if where == "search":
                if not query:
                    return {"success": False, "error": "搜索要填 query"}
                posts = await client.search(query, cfg.list_count)
                title = f"搜「{query}」（最近 7 天）"
            elif where == "user":
                name = username_of(query)
                if not name:
                    return {"success": False, "error": "query 要填 @用户名 或主页链接"}
                user = await client.user(name)
                posts = await client.user_posts(user["id"], cfg.list_count)
                title = f"{user['name']}（@{user['username']}）最近的帖子"
            elif where == "post":
                post_id = post_id_of(query)
                if not post_id:
                    return {"success": False, "error": "query 要填帖子链接或 ID"}
                posts = [await client.post(post_id)]
                title = "这条帖子"
            else:
                continuing = str(more).strip().lower() in ("true", "1", "yes", "是")
                if continuing and not self._next_token:
                    return {"success": True, "content": "时间线已经刷到底了（或者还没开始刷），不用 more 再刷一次就是最新的。"}
                posts, self._next_token = await client.timeline(cfg.list_count, replies=cfg.include_replies, next_token=self._next_token if continuing else "")
                title = "你的时间线" + ("（接着往下）" if continuing else "")
        except (AccountPaused, QuotaExceeded) as exc:
            return {"success": False, "error": str(exc)}
        except XError as exc:
            self._last_error = str(exc)
            self.ctx.logger.warning("X：%s 失败：%s %s", where, exc, exc.detail)
            return {"success": False, "error": f"没刷出来：{exc}"}
        return await self._render(title, posts, keep_all=where == "post")

    @Tool(
        REPLIES_TOOL,
        description=REPLIES_DESCRIPTION,
        parameters=[ToolParameterInfo(name="post", param_type=ToolParamType.STRING, description="帖子 ID 或链接")],
        chat_scope="private",
    )
    async def tool_replies(self, post: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        assert self._client is not None
        post_id = post_id_of(post)
        if not post_id:
            return {"success": False, "error": "post 要填帖子链接或 ID"}
        try:
            posts = await self._client.replies(post_id, self.config.browse.list_count)
        except (AccountPaused, QuotaExceeded) as exc:
            return {"success": False, "error": str(exc)}
        except XError as exc:
            self._last_error = str(exc)
            self.ctx.logger.warning("X：看回复失败：%s %s", exc, exc.detail)
            return {"success": False, "error": f"回复没打开：{exc}"}
        original = self._posts.get(post_id)
        title = f"{original.author or '这条帖子'}那条帖子下面的回复" if original else "这条帖子下面的回复"
        return await self._render(title, posts, keep_all=False, empty="这条帖子最近 7 天没人回复（或者回复太早，找不到了）。")

    async def _render(self, title: str, posts: List[Post], *, keep_all: bool, empty: str = "") -> Dict[str, Any]:
        shown = posts if keep_all else [p for p in posts if self._keep(p)]
        for p in posts:
            self._posts[p.id] = p
        for old in list(self._posts)[:-POST_KEEP]:
            del self._posts[old]
        if not shown:
            return {"success": True, "content": empty or f"{title}：这次什么都没刷到。"}
        images: Dict[str, str] = {}
        lines = [f"{title}："]
        for i, p in enumerate(shown, start=1):
            lines.append(self._post_line(i, p, images))
        self._images.update(images)
        for old in list(self._images)[:-IMAGE_KEEP]:  # 只记最近这些张，单次编号的图不会被挤掉
            del self._images[old]
        items = await self._image_items(images)
        if images:
            lines.append("（图在下面；想收藏成表情包就用 x_save_emoji，填图的编号）")
        return {"success": True, "content": "\n".join(lines), "content_items": items}

    def _keep(self, post: Post) -> bool:
        cfg = self.config.browse
        blocked_users = {u.strip().lstrip("@").lower() for u in cfg.blocked_users if u.strip()}
        if post.username.lower() in blocked_users:
            return False
        return not any(word and word in post.text for word in cfg.blocked_keywords)

    def _post_line(self, index: int, post: Post, images: Dict[str, str]) -> str:
        marks = []
        for url in post.images:
            if len(images) >= IMAGE_PER_CALL:
                marks.append("[图]")
                continue
            self._image_seq += 1
            name = f"图{self._image_seq}"
            images[name] = url
            marks.append(f"[{name}]")
        who = f"{post.author}（@{post.username}）" if post.username else post.author or "某人"
        kind = "（回复）" if post.reply else "（引用了别的帖子）" if post.quoted else ""
        stats = " ".join(s for s in (f"❤️{post.likes}" if post.likes else "", f"🔁{post.reposts}" if post.reposts else "", f"💬{post.replies}" if post.replies else "") if s)
        head = " · ".join(s for s in (who + kind, _ago(post.created_at), stats) if s)
        text = post.text if len(post.text) <= TEXT_LIMIT else post.text[:TEXT_LIMIT] + "…"
        return f"{index}. {head}\n   {text}{' ' + ' '.join(marks) if marks else ''}\n   ID {post.id}"

    async def _image_items(self, images: Dict[str, str]) -> List[Dict[str, Any]]:
        """把这次的图缩小后交给她看（最多 images_per_call 张）。"""
        assert self._client is not None
        items: List[Dict[str, Any]] = []
        for name, url in list(images.items())[: self.config.browse.images_per_call]:
            try:
                raw = await self._client.download_image(url)
                items.append({"type": "image", "data": _thumbnail(raw), "mime_type": "image/jpeg", "name": name})
            except Exception as exc:  # noqa: BLE001 - 一张图看不了不影响别的
                self.ctx.logger.info("X：图 %s 没下载成（%s）", name, exc)
        return items

    # ── 操作 ──────────────────────────────────────

    @Tool(
        ACTION_TOOL,
        description=ACTION_DESCRIPTION,
        parameters=[
            ToolParameterInfo(name="post", param_type=ToolParamType.STRING, description="帖子 ID 或链接；关注时也可以填 @用户名"),
            ToolParameterInfo(name="action", param_type=ToolParamType.STRING, description="点赞、收藏 或 关注"),
        ],
        chat_scope="private",
    )
    async def tool_action(self, post: str = "", action: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if error := self._usable():
            return {"success": False, "error": error}
        client = self._client
        assert client is not None
        key = ACTION_KEYS.get(str(action or "").strip().replace(" ", ""))
        if key is None:
            return {"success": False, "error": "action 只能是 点赞、收藏、关注"}
        if not getattr(self.config.account, key):
            return {"success": False, "error": f"{ACTION_NAMES[key]}没开放"}
        if reason := client.account.blocked("actions"):
            return {"success": False, "error": f"X 现在不让你的账号点赞、收藏、关注（{reason}），先别用了"}
        post_id = post_id_of(post)
        target = ""
        try:
            if key == "follow":
                target, what = await self._follow_target(post, post_id)
            else:
                if not post_id:
                    return {"success": False, "error": "post 要填帖子链接或 ID"}
                target = post_id
                known = self._posts.get(post_id)
                what = f"{known.author or '这条'}的帖子" if known else "这条帖子"
            async with self._action_lock:
                await asyncio.sleep(client.account.check_action(key, target))
                if reason := client.account.blocked("actions"):  # 排队期间前一个操作可能刚被 X 拒绝
                    return {"success": False, "error": f"X 现在不让你的账号点赞、收藏、关注（{reason}），先别用了"}
                client.account.record_action(key, target)  # 先记下：中途断网、被取消、进程退出都不会绕过上限
                try:
                    if key == "like":
                        await client.like(target)
                    elif key == "bookmark":
                        await client.bookmark(target)
                    else:
                        await client.follow(target)
                except (AccountPaused, QuotaExceeded):
                    client.account.undo_action(key, target)  # 请求没发出去
                    raise
                except XError as exc:
                    if not exc.uncertain:
                        client.account.undo_action(key, target)  # X 明确没做
                    raise
        except (AccountPaused, QuotaExceeded) as exc:
            return {"success": False, "error": str(exc)}
        except XError as exc:
            self._last_error = str(exc)
            self.ctx.logger.warning("X：%s失败：%s %s", ACTION_NAMES[key], exc, exc.detail)
            if exc.uncertain and target:
                return {"success": False, "error": f"{ACTION_NAMES[key]}不知道成没成（{exc}），先不重复做了"}
            if exc.status == 403:
                client.account.block("actions", str(exc))
                self.ctx.logger.warning("X：X 不让她的账号点赞收藏关注（%s），先停用，主人 /x_resume 后再试", exc)
                return {"success": False, "error": f"X 不让你的账号做这些操作（{exc}），先别用了"}
            return {"success": False, "error": f"{ACTION_NAMES[key]}没成功：{exc}"}
        self.ctx.logger.info("X：她%s了 %s", ACTION_NAMES[key], target)
        return {"success": True, "content": f"已经{ACTION_NAMES[key]}：{what}。"}

    async def _follow_target(self, raw: str, post_id: str) -> tuple:
        """关注谁：帖子的作者（读过的帖子不用再花钱查），或者 @用户名。"""
        assert self._client is not None
        if post_id:
            known = self._posts.get(post_id) or await self._client.post(post_id)
            self._posts[post_id] = known
            if not known.author_id:
                raise XError("找不到这条帖子的作者")
            return known.author_id, f"{known.author}（@{known.username}）" if known.username else "这条帖子的作者"
        name = username_of(raw)
        if not name:
            raise XError("post 要填帖子链接、ID 或 @用户名")
        user = await self._client.user(name)
        return user["id"], f"{user['name']}（@{user['username']}）"

    @Tool(
        EMOJI_TOOL,
        description=EMOJI_DESCRIPTION,
        parameters=[ToolParameterInfo(name="image", param_type=ToolParamType.STRING, description="图的编号，例如 图1")],
        chat_scope="private",
    )
    async def tool_save_emoji(self, image: str = "", **kwargs: Any) -> Dict[str, Any]:
        del kwargs
        if not self.config.plugin.enabled or self._client is None:
            return {"success": False, "error": "X 的功能现在关着"}
        name = str(image or "").strip().replace("[", "").replace("]", "")
        if name.isdigit():
            name = f"图{name}"
        url = self._images.get(name)
        if not url:
            return {"success": False, "error": "没有这张图（只能收藏刚才在 X 上看到的图）"}
        try:
            raw = await self._client.download_image(url)
            result = await self.ctx.emoji.register_emoji(base64.b64encode(raw).decode())
        except XError as exc:
            return {"success": False, "error": f"图没下载下来：{exc}"}
        if isinstance(result, dict) and result.get("success") is False:
            return {"success": False, "error": str(result.get("message") or result.get("error") or "没收藏成")}
        self.ctx.logger.info("X：她把%s收藏成了表情包", name)
        description = result.get("description") if isinstance(result, dict) else ""
        return {"success": True, "content": "收藏好了" + (f"（MaiBot 给它的描述：{description}）" if description else "") + "，以后聊天可以用。"}

    # ── 授权 ──────────────────────────────────────

    @Command("x_login", description="授权她自己的 X 账号", pattern=r"^/x_login$")
    async def handle_login(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        client = self._client
        if client is None or not self.config.plugin.enabled:
            text = "X 的功能现在关着。"
        elif not client.app.client_id:
            text = "先在 WebUI 的插件配置里填 X 开发者应用的 Client ID。"
        else:
            url, self._pending = client.authorize_url()
            text = (
                "用登录着她 X 账号的手机打开这个链接，点「Authorize app / 授权」：\n"
                f"{url}\n\n"
                "授权后页面会跳到一个打不开的网址（以 127.0.0.1 开头），这是正常的。"
                "把地址栏里那个网址整个复制，发「/x_auth 网址」给我（10 分钟内有效）。"
            )
        # 授权链接和提示都不存进聊天记录：不然会被当成聊天内容写进记忆
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "已处理", True

    @Command("x_auth", description="把 X 授权后跳转的网址发回来", pattern=r"^/x_auth\s+(?P<url>\S+)\s*$")
    async def handle_auth(self, stream_id: str = "", matched_groups: Optional[Dict[str, Any]] = None, **kwargs: Any) -> tuple:
        del kwargs
        client, pending = self._client, self._pending
        if client is None:
            text = "X 的功能现在关着。"
        elif pending is None:
            text = "没有等待中的授权，先发 /x_login。"
        else:
            self._pending = None  # 一个授权链接只用一次：同时发两次 /x_auth 不会互相写乱
            try:
                me = await client.finish_login(str((matched_groups or {}).get("url") or ""), pending)
                self._next_token = ""
                text = f"授权好了：{me['name']}（@{me['username']}）。"
                self.ctx.logger.info("X：她的账号授权好了（@%s）", me["username"])
            except (XError, AccountPaused, QuotaExceeded) as exc:
                if self._pending is None:
                    self._pending = pending  # 网址贴错了可以再贴一次（过期后会提示重新 /x_login）
                text = f"授权没完成：{exc}"
                self.ctx.logger.warning("X：授权没完成：%s", exc)
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "已处理", True

    @Command("x_resume", description="X 被暂停后，确认没问题了恢复", pattern=r"^/x_resume$")
    async def handle_resume(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        account = self._account
        reason = account.paused if account is not None else ""
        if account is not None and account.resume():
            text = f"已恢复（之前暂停的原因：{reason}）。" if reason else "好，让她再试试。"
            self.ctx.logger.info("X：主人恢复了她的账号（之前：%s）", reason or "功能被拒绝或冷却")
        else:
            text = "X 没有暂停。"
        await self.ctx.send.text(text, stream_id, storage_message=False)
        return True, "已处理", True

    # ── 状态命令 ──────────────────────────────────

    @Command("x_status", description="查看 X 插件的状态", pattern=r"^/x_status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> tuple:
        del kwargs
        # 不存进聊天记录：否则 MaiBot 重启后会把状态当成 AI 说过的话
        await self.ctx.send.text(self.status_text(), stream_id, storage_message=False)
        return True, "状态已显示", True

    def status_text(self) -> str:
        cfg = self.config
        lines = [f"🐦 X v{PLUGIN_VERSION}", "状态：" + ("已启用" if cfg.plugin.enabled else "已关闭")]
        account = self._account
        if not cfg.app.client_id.strip():
            lines.append("X 开发者应用：未填写 Client ID")
        if account is not None:
            if account.logged_in:
                lines.append(f"她的账号：✅ @{account.username}")
            else:
                lines.append("她的账号：未授权（发 /x_login）")
            month = account.month()
            lines.append(
                f"这个月：已花 ${account.spent_usd:.3f} / ${account.prices.monthly:.2f}"
                f"（读了帖子 {month.get('posts', 0)} 条、账号 {month.get('users', 0)} 个、操作 {month.get('actions', 0)} 次；按 X 公布的价格估算，实际以开发者后台为准）"
            )
            day = account.today()
            done = "，".join(f"{ACTION_NAMES[k]} {v}" for k, v in (day.get("actions") or {}).items() if v)
            lines.append(f"今天：请求 {day.get('requests', 0)} / {account.limits.requests_per_day}" + (f"；{done}" if done else ""))
            if account.paused:
                lines.append(f"⛔ 已暂停：{account.paused}（处理好后发 /x_resume）")
            if wait := account.cooldown:
                lines.append(f"⏳ X 说太频繁，还要等约 {max(1, wait // 60)} 分钟（会自己恢复）")
            if reason := account.blocked("actions"):
                lines.append(f"🚫 X 不让她点赞、收藏、关注：{reason}（发 /x_resume 让她再试）")
        if self._last_error:
            lines.append(f"最近一次失败：{self._last_error}")
        return "\n".join(lines)


def _ago(created_at: str) -> str:
    try:
        moment = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    minutes = int((datetime.now(timezone.utc) - moment).total_seconds() // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    if minutes < 24 * 60:
        return f"{minutes // 60} 小时前"
    return f"{minutes // (24 * 60)} 天前"


def _thumbnail(raw: bytes) -> str:
    """缩到最长边 IMAGE_MAX_SIDE 的 JPEG（base64），动图取第一帧。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        image.seek(0)
        frame = image.convert("RGB")
        frame.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buffer = io.BytesIO()
        frame.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode()


def create_plugin() -> XBrowsePlugin:
    """MaiBot 插件工厂函数。"""
    return XBrowsePlugin()
