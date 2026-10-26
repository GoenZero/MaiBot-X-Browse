"""本地假的 X 接口：授权换令牌、续期、她的账号、时间线、搜索、某人的帖子、单条帖子、点赞收藏关注、图片。"""

from __future__ import annotations

from typing import Any, Dict, List

import asyncio
import io

from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

ME = {"id": "42", "username": "dafeiyu", "name": "大肥鱼"}


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (900, 600), (30, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeX:
    def __init__(self) -> None:
        self.base = ""
        self.token_forms: List[Dict[str, str]] = []
        self.requests: List[str] = []
        self.posts: List[Dict[str, Any]] = []
        self.access = "at-1"
        self.refresh = "rt-1"
        self.issued = 0
        self.reject_token = ""  # 这个 access token 返回 401（模拟过期）
        self.status = 200  # 读接口统一返回的状态（测 429 / 402 / 403）
        self.action_status = 200
        self.garbage = False  # 返回 200 但内容不是 JSON
        self.drop_action = False  # 操作做成了但连接断了
        self.headers: Dict[str, str] = {}

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/2/oauth2/token", self.token)
        app.router.add_get("/2/users/me", self.me)
        app.router.add_get("/2/users/42/timelines/reverse_chronological", self.timeline)
        app.router.add_get("/2/tweets/search/recent", self.search)
        app.router.add_get("/2/users/by/username/{name}", self.by_username)
        app.router.add_get("/2/users/{uid}/tweets", self.user_tweets)
        app.router.add_get("/2/tweets/{pid}", self.single)
        for path in ("/2/users/42/likes", "/2/users/42/bookmarks", "/2/users/42/following"):
            app.router.add_post(path, self.action)
        app.router.add_get("/media/pic.png", self.picture)
        return app

    def page(self, items: List[Dict[str, Any]], **meta: Any) -> Dict[str, Any]:
        return {
            "data": items,
            "includes": {
                "users": [{"id": "7", "username": "catlover", "name": "猫猫"}, {"id": "8", "username": "seaside", "name": "海边的人"}],
                "media": [{"media_key": "m1", "type": "photo", "url": f"{self.base}/media/pic.png"}],
            },
            "meta": {"result_count": len(items), **meta},
        }

    @staticmethod
    def post(pid: str, author: str, text: str, **extra: Any) -> Dict[str, Any]:
        return {
            "id": pid,
            "author_id": author,
            "text": text,
            "created_at": "2026-10-27T00:00:00.000Z",
            "public_metrics": {"like_count": 120, "retweet_count": 3, "quote_count": 1, "reply_count": 9},
            **extra,
        }

    def check(self, request: web.Request) -> web.Response | None:
        self.requests.append(f"{request.method} {request.path}")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token != self.access or token == self.reject_token:
            return web.json_response({"title": "Unauthorized", "detail": "Unauthorized"}, status=401)
        if self.status != 200 and request.method == "GET":
            return web.json_response({"title": "Error", "detail": f"status {self.status}"}, status=self.status, headers=self.headers)
        if self.garbage and request.method == "GET":
            return web.Response(text="<html>proxy error</html>", content_type="text/html")
        return None

    async def token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.token_forms.append(form)
        if form.get("grant_type") == "authorization_code":
            if form.get("code") != "good-code" or not form.get("code_verifier") or form.get("client_id") != "cid":
                return web.json_response({"error": "invalid_request", "error_description": "Value passed for the authorization code was invalid."}, status=400)
        elif form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") != self.refresh:
                return web.json_response({"error": "invalid_request", "error_description": "Value passed for the token was invalid."}, status=400)
        self.issued += 1
        self.access, self.refresh = f"at-{self.issued + 1}", f"rt-{self.issued + 1}"
        return web.json_response({"token_type": "bearer", "expires_in": 7200, "access_token": self.access, "refresh_token": self.refresh, "scope": "tweet.read users.read"})

    async def me(self, request: web.Request) -> web.Response:
        return self.check(request) or web.json_response({"data": ME})

    async def timeline(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        assert request.query["exclude"] == "replies,retweets"
        if request.query.get("pagination_token") == "p2":
            return web.json_response(self.page([self.post("103", "8", "第二页的帖子")]))
        return web.json_response(self.page([
            self.post("101", "7", "猫跳上桌子又假装无事发生 &amp; 看着我", attachments={"media_keys": ["m1"]}),
            self.post("102", "8", "今天海边风好大", note_tweet={"text": "今天海边风好大，长帖完整版"}),
        ], next_token="p2"))

    async def search(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        assert int(request.query["max_results"]) >= 10
        query = request.query["query"]
        if query == "in_reply_to_tweet_id:101":
            return web.json_response(self.page([self.post("201", "8", "笑死，猫：我没摔", referenced_tweets=[{"type": "replied_to", "id": "101"}])]))
        assert query == "(鲸鱼) -is:retweet"
        return web.json_response(self.page([self.post("301", "8", "鲸鱼顶着海带游过去了")]))

    async def by_username(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        if request.match_info["name"] != "seaside":
            return web.json_response({"errors": [{"detail": "Could not find user"}]})
        return web.json_response({"data": {"id": "8", "username": "seaside", "name": "海边的人"}})

    async def user_tweets(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        assert int(request.query["max_results"]) >= 5
        return web.json_response(self.page([self.post("401", request.match_info["uid"], "海边日落")]))

    async def single(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        if request.match_info["pid"] == "999":
            return web.json_response({"errors": [{"detail": "Could not find tweet with id: [999]."}]})
        return web.json_response(self.page([self.post(request.match_info["pid"], "7", "单条帖子")]))

    async def action(self, request: web.Request) -> web.Response:
        if denied := self.check(request):
            return denied
        body = await request.json()
        self.posts.append({"path": request.path, **body})
        if self.drop_action:
            request.transport.close()  # 做成了，但回复没送到
            await asyncio.sleep(0.05)
            return web.json_response({"data": {"done": True}})
        if self.action_status != 200:
            return web.json_response({"title": "Forbidden", "detail": "You are not permitted to perform this action."}, status=self.action_status)
        return web.json_response({"data": {"done": True}})

    async def picture(self, request: web.Request) -> web.Response:
        return web.Response(body=_png(), content_type="image/png")


async def start(world: FakeX) -> TestServer:
    server = TestServer(world.app())
    await server.start_server()
    world.base = str(server.make_url("")).rstrip("/")
    return server
