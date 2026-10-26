"""X 官方接口（API v2）：OAuth 2.0 授权（PKCE）、她的时间线、搜索、某人的帖子、回复、点赞收藏关注、下载图片。

每个请求都先过账号守卫（暂停、冷却、每日请求数、间隔）和花费预扣，拿到结果后按实际读到的条数结算。
授权流程参考 dsh-x-connect（PKCE、只用 Client ID 也能授权），出错处理参考 elizaOS 的推特插件（429 按 X 给的时间冷却），代码为重写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import asyncio
import base64
import hashlib
import html
import re
import secrets
import time

import aiohttp

from .account import Account, AccountPaused

SCOPES = "tweet.read users.read like.write bookmark.write follows.write offline.access"
USER_AGENT = "MaiBot-X-Browse"
POST_FIELDS = {
    "tweet.fields": "created_at,public_metrics,note_tweet,referenced_tweets,author_id,attachments",
    "expansions": "author_id,attachments.media_keys",
    "user.fields": "username,name",
    "media.fields": "type,url,preview_image_url",
}
_POST_LINK = re.compile(r"(?:x|twitter)\.com/(?:[A-Za-z0-9_]+|i/web)/status(?:es)?/(\d{1,25})", re.I)
_USER_LINK = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#]|$)", re.I)
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# 授权链接多久内有效（X 的授权码本身只有 30 秒，这里放宽给主人复制粘贴的时间）
PENDING_SECONDS = 600


class XError(Exception):
    """X 那边出的问题（message 给 AI 看，detail 只进日志）。"""

    def __init__(self, message: str, detail: str = "", *, status: int = 0, uncertain: bool = False) -> None:
        super().__init__(message)
        self.detail = detail
        self.status = status
        self.uncertain = uncertain  # 请求可能已经被 X 处理了（断网、超时、返回内容看不懂）


@dataclass
class AppSettings:
    client_id: str
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:56130/callback"


@dataclass
class Post:
    id: str
    text: str
    author_id: str = ""
    author: str = ""
    username: str = ""
    created_at: str = ""
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    images: List[str] = field(default_factory=list)
    reply: bool = False
    quoted: bool = False

    @property
    def link(self) -> str:
        return f"https://x.com/{self.username or 'i/web'}/status/{self.id}"


@dataclass
class Pending:
    verifier: str
    state: str
    created: float


def post_id_of(text: str) -> str:
    """帖子链接或 ID → 帖子 ID；认不出返回空。"""
    text = str(text or "").strip()
    if match := _POST_LINK.search(text):
        return match.group(1)
    return text if text.isdigit() and len(text) <= 25 else ""


def username_of(text: str) -> str:
    """@用户名、用户名或主页链接 → 用户名；认不出返回空。"""
    text = str(text or "").strip()
    if match := _USER_LINK.search(text):
        name = match.group(1)
        return "" if name.lower() in {"i", "home", "search", "explore"} else name
    text = text.lstrip("@")
    return text if _USERNAME.match(text) else ""


def parse_posts(payload: Dict[str, Any]) -> List[Post]:
    data = payload.get("data")
    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    users = {str(u.get("id")): u for u in includes.get("users") or [] if isinstance(u, dict)}
    media = {str(m.get("media_key")): m for m in includes.get("media") or [] if isinstance(m, dict)}
    posts: List[Post] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        note = item.get("note_tweet") if isinstance(item.get("note_tweet"), dict) else {}
        metrics = item.get("public_metrics") if isinstance(item.get("public_metrics"), dict) else {}
        author = users.get(str(item.get("author_id") or ""), {})
        keys = (item.get("attachments") or {}).get("media_keys") or [] if isinstance(item.get("attachments"), dict) else []
        images = []
        for key in keys:
            m = media.get(str(key)) or {}
            url = m.get("url") or m.get("preview_image_url")  # 视频和动图只有封面
            if url:
                images.append(str(url))
        refs = {str(r.get("type")) for r in item.get("referenced_tweets") or [] if isinstance(r, dict)}
        posts.append(
            Post(
                id=str(item["id"]),
                text=html.unescape(str(note.get("text") or item.get("text") or "")).strip(),
                author_id=str(item.get("author_id") or ""),
                author=str(author.get("name") or ""),
                username=str(author.get("username") or ""),
                created_at=str(item.get("created_at") or ""),
                likes=int(metrics.get("like_count") or 0),
                reposts=int(metrics.get("retweet_count") or 0) + int(metrics.get("quote_count") or 0),
                replies=int(metrics.get("reply_count") or 0),
                images=images,
                reply="replied_to" in refs,
                quoted="quoted" in refs,
            )
        )
    return posts


def read_usage(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    """这次读到了哪些帖子和账号（X 按这些收钱）。"""
    data = payload.get("data")
    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    posts = [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")]
    posts += [str(t.get("id")) for t in includes.get("tweets") or [] if isinstance(t, dict) and t.get("id")]
    users = [str(u.get("id")) for u in includes.get("users") or [] if isinstance(u, dict) and u.get("id")]
    return {"posts": posts, "users": users}


def user_usage(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"users": [str(data["id"])] if data.get("id") else []}


def action_usage(payload: Dict[str, Any]) -> Dict[str, Any]:
    del payload
    return {"actions": 1}


class XClient:
    API = "https://api.x.com"
    AUTHORIZE = "https://x.com/i/oauth2/authorize"
    IMAGE_HOSTS: Tuple[str, ...] = ("pbs.twimg.com",)

    def __init__(self, session: aiohttp.ClientSession, account: Account, app: AppSettings, logger: Any = None) -> None:
        self._session = session
        self.account = account
        self.app = app
        self.logger = logger
        self._refresh_lock = asyncio.Lock()

    # ── 授权 ──────────────────────────────────────

    def authorize_url(self) -> Tuple[str, Pending]:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        pending = Pending(verifier=verifier, state=secrets.token_urlsafe(16), created=time.time())
        query = {
            "response_type": "code",
            "client_id": self.app.client_id,
            "redirect_uri": self.app.redirect_uri,
            "scope": SCOPES,
            "state": pending.state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.AUTHORIZE}?{urlencode(query, quote_via=quote)}", pending

    async def finish_login(self, redirected: str, pending: Pending) -> Dict[str, str]:
        """主人把授权后跳转的网址粘回来：核对 state，用授权码换令牌，再查她的账号。"""
        if time.time() - pending.created > PENDING_SECONDS:
            raise XError("授权链接过期了，请重新发 /x_login")
        query = parse_qs(urlsplit(str(redirected or "").strip()).query)
        if query.get("error"):
            raise XError(f"授权没完成（{query['error'][0]}）")
        if (query.get("state") or [""])[0] != pending.state:
            raise XError("这个网址不是这次授权的（state 对不上），请重新发 /x_login")
        code = (query.get("code") or [""])[0]
        if not code:
            raise XError("网址里没有授权码，请把跳转后地址栏里的整个网址复制过来")
        token = await self._token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.app.redirect_uri, "code_verifier": pending.verifier}
        )
        async with self._refresh_lock:  # 别和正在进行的续期互相覆盖
            self.account.forget()
            self.account.save_tokens(token)
        self.account.resume(reason_contains="授权")  # 因为授权失效而暂停的，重新授权后解除（额度用完等别的原因不动）
        payload = await self._call("GET", "/2/users/me", params={"user.fields": "username,name"}, reserve_usd=self.account.prices.user_read, usage=user_usage, need_user=False)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data.get("id"):
            raise XError("授权好了，但查不到她的账号")
        me = {"id": str(data["id"]), "username": str(data.get("username") or ""), "name": str(data.get("name") or "")}
        self.account.save_profile(me["id"], me["username"], me["name"])
        return me

    async def _token(self, form: Dict[str, str]) -> Dict[str, Any]:
        auth = None
        if self.app.client_secret:
            auth = aiohttp.BasicAuth(self.app.client_id, self.app.client_secret)  # 机密客户端
        else:
            form = {**form, "client_id": self.app.client_id}  # 公开客户端（PKCE）
        try:
            async with self._session.post(
                f"{self.API}/2/oauth2/token",
                data=form,
                auth=auth,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                status = response.status
                try:
                    payload = await response.json(content_type=None)
                except ValueError:
                    payload = {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise XError(f"连接 X 失败（{type(exc).__name__}）") from exc
        except RuntimeError as exc:  # 插件刚重新加载、会话已关
            raise XError("插件刚重新加载，再试一次", f"token {exc}") from exc
        payload = payload if isinstance(payload, dict) else {}
        if status >= 400 or not payload.get("access_token"):
            reason = str(payload.get("error_description") or payload.get("error") or f"HTTP {status}")[:120]
            raise XError(f"X 没给授权：{reason}", f"token HTTP {status}", status=status or 400)
        return payload

    async def _access_token(self, failed: str = "") -> str:
        """有效的 access token；快过期或刚被拒（failed）就用 refresh token 换新的（新的 refresh token 立刻存下）。"""
        cred = self.account.credential()
        token = str(cred.get("access_token") or "")
        if token and token != failed and float(cred.get("expires_at") or 0) - 120 > time.time():
            return token
        async with self._refresh_lock:
            cred = self.account.credential()  # 等锁期间可能别的请求已经换好了
            token = str(cred.get("access_token") or "")
            if token and token != failed and float(cred.get("expires_at") or 0) - 120 > time.time():
                return token
            refresh = str(cred.get("refresh_token") or "")
            if not refresh:
                self.account.pause("授权失效了，需要主人重新 /x_login")
                raise AccountPaused("X 的授权失效了，等主人重新授权")
            try:
                fresh = await self._token({"grant_type": "refresh_token", "refresh_token": refresh})
            except XError as exc:
                if exc.status in (400, 401):
                    self.account.pause("授权失效了（续期被 X 拒绝），需要主人重新 /x_login")
                    raise AccountPaused("X 的授权失效了，等主人重新授权") from exc
                raise
            if str(self.account.credential().get("refresh_token") or "") != refresh:
                raise XError("她的 X 账号刚重新授权过，再试一次")  # 续期期间换了号，旧结果作废
            self.account.save_tokens(fresh)
            return str(fresh["access_token"])

    # ── 请求 ──────────────────────────────────────

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        reserve_usd: float,
        usage: Callable[[Dict[str, Any]], Dict[str, Any]],
        action: bool = False,
        need_user: bool = True,
    ) -> Dict[str, Any]:
        if need_user and not self.account.logged_in:
            raise XError("还没授权她的 X 账号")
        key = self.account.reserve(reserve_usd)
        lost = False
        settled = False
        try:
            token = ""
            for attempt in range(2):
                # 每次真正发请求（包括 401 后的重试）都过守卫：先拿到令牌，再计数、等间隔，紧挨着发送前再查一次暂停和冷却
                token = await self._access_token(failed=token)
                wait = self.account.before_request()
                if wait:
                    await asyncio.sleep(wait)
                self.account.check_open()
                lost = True
                status, headers, payload = await self._send(method, path, params, body, token)
                lost = False
                if status != 401 or attempt:
                    break
            if status >= 400:
                self._fail(status, headers, payload, path, action=action)
            if payload is None:  # 成功了却看不懂返回的内容：可能已经扣费，按预扣的算
                lost = True
                raise XError("X 返回的内容看不懂", f"{path} HTTP {status} 不是 JSON", status=status, uncertain=True)
            self.account.settle(key, **usage(payload))
            settled = True
            return payload
        finally:
            if not settled:
                self.account.settle(key, lost=lost)

    async def _send(self, method: str, path: str, params: Optional[Dict[str, Any]], body: Optional[Dict[str, Any]], token: str) -> Tuple[int, Any, Optional[Dict[str, Any]]]:
        """发一个请求；返回内容不是 JSON 对象时 payload 为 None。"""
        try:
            async with self._session.request(
                method,
                self.API + path,
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except ValueError:
                    payload = None
                return response.status, response.headers, payload if isinstance(payload, dict) else None
        except asyncio.TimeoutError as exc:
            raise XError("连接 X 超时", f"{path} 超时", uncertain=True) from exc
        except aiohttp.ClientError as exc:
            raise XError(f"连接 X 失败（{type(exc).__name__}）", f"{path} {type(exc).__name__}", uncertain=True) from exc
        except RuntimeError as exc:  # 等待期间插件重新加载、会话已关
            raise XError("插件刚重新加载，再试一次", f"{path} {exc}", uncertain=True) from exc

    def _fail(self, status: int, headers: Any, payload: Optional[Dict[str, Any]], path: str, *, action: bool) -> None:
        detail = _detail(payload or {})
        where = f"{path} HTTP {status} {detail}"
        if status == 429:
            reset = str(headers.get("x-rate-limit-reset") or "")
            self.account.cool_down(float(reset) if reset.isdigit() else time.time() + 900)
            raise XError("X 说刷得太频繁了，过一会儿再来", where, status=status)
        if status == 401:
            self.account.pause("授权失效了（HTTP 401），需要主人重新 /x_login")
            raise AccountPaused("X 的授权失效了，等主人重新授权")
        if status == 402:
            self.account.pause(f"X 开发者账号的额度用完了（HTTP 402：{detail}）")
            raise AccountPaused("X 开发者账号的额度用完了，等主人处理")
        if status == 403 and not action:
            self.account.pause(f"X 拒绝访问（HTTP 403：{detail}）")
            raise AccountPaused(f"X 拒绝访问了（{detail}），等主人处理")
        if status == 404:
            raise XError("找不到（可能被删了，或者是锁了的账号）", where, status=status)
        raise XError(f"X 返回 HTTP {status}：{detail}", where, status=status, uncertain=status >= 500)  # 服务器出错时操作可能已经做了

    # ── 读 ────────────────────────────────────────

    def _read_reserve(self, count: int) -> float:
        prices = self.account.prices
        return count * (prices.post_read + prices.user_read)  # 最坏情况：每条帖子的作者都是没读过的账号

    async def timeline(self, count: int, *, replies: bool = False, next_token: str = "") -> Tuple[List[Post], str]:
        """她关注的人发的帖子（时间倒序）。"""
        count = max(1, min(100, count))
        params: Dict[str, Any] = {**POST_FIELDS, "max_results": count, "exclude": "retweets" if replies else "replies,retweets"}
        if next_token:
            params["pagination_token"] = next_token
        payload = await self._call(
            "GET", f"/2/users/{self.account.user_id}/timelines/reverse_chronological", params=params, reserve_usd=self._read_reserve(count), usage=read_usage
        )
        return parse_posts(payload), str((payload.get("meta") or {}).get("next_token") or "")

    async def search(self, keyword: str, count: int) -> List[Post]:
        """最近 7 天的帖子里搜（X 规定一次最少 10 条）。"""
        count = max(10, min(100, count))
        params = {**POST_FIELDS, "query": f"({keyword}) -is:retweet", "max_results": count, "sort_order": "relevancy"}
        payload = await self._call("GET", "/2/tweets/search/recent", params=params, reserve_usd=self._read_reserve(count), usage=read_usage)
        return parse_posts(payload)

    async def replies(self, post_id: str, count: int) -> List[Post]:
        """直接回复这条帖子的（只能找到最近 7 天的）。不用 conversation_id：那是整串讨论的根帖，查回复的回复会查空。"""
        count = max(10, min(100, count))
        params = {**POST_FIELDS, "query": f"in_reply_to_tweet_id:{post_id}", "max_results": count, "sort_order": "relevancy"}
        payload = await self._call("GET", "/2/tweets/search/recent", params=params, reserve_usd=self._read_reserve(count), usage=read_usage)
        return parse_posts(payload)

    async def user(self, username: str) -> Dict[str, str]:
        payload = await self._call(
            "GET", f"/2/users/by/username/{username}", params={"user.fields": "username,name"}, reserve_usd=self.account.prices.user_read, usage=user_usage
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if not data.get("id"):
            raise XError(f"找不到 @{username} 这个账号", _detail(payload))
        return {"id": str(data["id"]), "username": str(data.get("username") or username), "name": str(data.get("name") or "")}

    async def user_posts(self, user_id: str, count: int) -> List[Post]:
        """某个账号最近发的帖子（X 规定一次最少 5 条）。"""
        count = max(5, min(100, count))
        params = {**POST_FIELDS, "max_results": count, "exclude": "retweets,replies"}
        payload = await self._call("GET", f"/2/users/{user_id}/tweets", params=params, reserve_usd=self._read_reserve(count), usage=read_usage)
        return parse_posts(payload)

    async def post(self, post_id: str) -> Post:
        payload = await self._call("GET", f"/2/tweets/{post_id}", params=POST_FIELDS, reserve_usd=self._read_reserve(1), usage=read_usage)
        posts = parse_posts(payload)
        if not posts:
            raise XError("找不到这条帖子（可能被删了，或者是锁了的账号）", _detail(payload))
        return posts[0]

    # ── 操作 ──────────────────────────────────────

    async def like(self, post_id: str) -> None:
        await self._act(f"/2/users/{self.account.user_id}/likes", {"tweet_id": post_id})

    async def bookmark(self, post_id: str) -> None:
        await self._act(f"/2/users/{self.account.user_id}/bookmarks", {"tweet_id": post_id})

    async def follow(self, user_id: str) -> None:
        await self._act(f"/2/users/{self.account.user_id}/following", {"target_user_id": user_id})

    async def _act(self, path: str, body: Dict[str, Any]) -> None:
        payload = await self._call("POST", path, body=body, reserve_usd=self.account.prices.action, usage=action_usage, action=True)
        if not isinstance(payload.get("data"), dict):
            raise XError(f"X 没确认（{_detail(payload)}）", f"{path} {payload}", uncertain=True)

    # ── 图片 ──────────────────────────────────────

    async def download_image(self, url: str, max_bytes: int = 8 * 1024 * 1024) -> bytes:
        """下载帖子里的图（只去 X 自己的图片域名，不带授权信息）。"""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme not in ("https", "http") or not any(host == h or host.endswith("." + h) for h in self.IMAGE_HOSTS):
            raise XError("不是 X 的图片地址")
        if not parts.query and host.endswith("pbs.twimg.com"):
            url = f"{url}?name=small"  # 小图（最长边约 680）就够看
        try:
            async with self._session.get(url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status >= 400:
                    raise XError(f"图片下载失败：HTTP {response.status}")
                body = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise XError("图片太大")
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
            raise XError(f"图片下载失败（{type(exc).__name__}）") from exc
        return bytes(body)


def _detail(payload: Dict[str, Any]) -> str:
    errors = payload.get("errors")
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    text = payload.get("detail") or payload.get("title") or first.get("detail") or first.get("message") or first.get("title") or ""
    return str(text)[:120]
