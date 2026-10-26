"""把插件放进假的 MaiBot 环境、连本地假的 X：授权、刷帖子、花费、续期、暂停、操作、表情包。"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncio
import copy
import os
import re
import time

import pytest
from maibot_sdk.context import PluginContext, PluginPaths

import x_browse_under_test as plugin_module
from x_browse_under_test.core.account import Account, Limits, Prices, QuotaExceeded
from x_browse_under_test.core.xapi import XClient, post_id_of, username_of

from fake_x import FakeX, start

CONFIG = {
    "plugin": {"enabled": True, "config_version": "1.0.0"},
    "app": {"client_id": "cid", "client_secret": "", "redirect_uri": "http://127.0.0.1:56130/callback"},
    "budget": {"monthly_usd": 10.0},
}


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def rpc(self, method: str, plugin_id: str, payload: dict[str, Any], timeout_ms: int | None = None) -> Any:
        self.calls.append((method, payload))
        if method == "cap.call" and payload["capability"] == "emoji.register":
            return {"success": True, "description": "一只蓝色的图"}
        return {"success": True}

    def sent_texts(self) -> list[str]:
        return [str(p["args"].get("text")) for name, p in self.calls if name == "cap.call" and p["capability"] == "send.text"]


def make_plugin(tmp_path, host: FakeHost, config: dict | None = None) -> Any:
    plugin = plugin_module.create_plugin()
    plugin._set_context(PluginContext("com.goenzero.x_browse", host.rpc, PluginPaths(data_dir=tmp_path / "data", runtime_dir=tmp_path / "rt")))
    plugin.set_plugin_config(copy.deepcopy(config or CONFIG))
    return plugin


def with_x(monkeypatch, test):
    async def main():
        world = FakeX()
        server = await start(world)
        monkeypatch.setattr(XClient, "API", world.base)
        monkeypatch.setattr(XClient, "IMAGE_HOSTS", ("127.0.0.1",))
        try:
            await test(world)
        finally:
            await server.close()

    asyncio.run(main())


async def login(plugin, host) -> None:
    await plugin.handle_login(stream_id="s1")
    link = re.search(r"https?://\S+", host.sent_texts()[-1]).group(0)
    state = parse_qs(urlsplit(link).query)["state"][0]
    await plugin.handle_auth(stream_id="s1", matched_groups={"url": f"http://127.0.0.1:56130/callback?state={state}&code=good-code"})
    plugin._account.limits = Limits(request_interval=0, action_interval=0)


def test_login_browse_and_budget(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        assert (await plugin.tool_browse(source="时间线"))["error"].startswith("还没授权")
        await plugin.handle_login(stream_id="s1")
        link = re.search(r"https?://\S+", host.sent_texts()[-1]).group(0)
        query = parse_qs(urlsplit(link).query)
        assert query["code_challenge_method"] == ["S256"] and "offline.access" in query["scope"][0] and query["client_id"] == ["cid"]
        # state 对不上就不换令牌
        await plugin.handle_auth(stream_id="s1", matched_groups={"url": "http://127.0.0.1:56130/callback?state=evil&code=good-code"})
        assert "state 对不上" in host.sent_texts()[-1] and world.token_forms == []
        await plugin.handle_auth(stream_id="s1", matched_groups={"url": f"http://127.0.0.1:56130/callback?state={query['state'][0]}&code=good-code"})
        assert "授权好了：大肥鱼（@dafeiyu）" in host.sent_texts()[-1]
        credential = tmp_path / "data" / "account" / "credential.json"
        if os.name == "posix":
            assert credential.stat().st_mode & 0o777 == 0o600
        plugin._account.limits = Limits(request_interval=0, action_interval=0)
        spent_after_login = plugin._account.spent_usd
        assert spent_after_login == pytest.approx(0.010)

        result = await plugin.tool_browse(source="时间线")
        assert result["success"], result
        text = result["content"]
        assert "猫猫（@catlover）" in text and "& 看着我" in text and "长帖完整版" in text and "[图1]" in text and "ID 101" in text
        assert len(result["content_items"]) == 1 and result["content_items"][0]["name"] == "图1"
        assert "at-" not in text and "rt-" not in text  # 令牌不给模型看
        # 两条帖子 + 两个作者
        assert plugin._account.spent_usd == pytest.approx(spent_after_login + 2 * 0.005 + 2 * 0.010)
        # 同一天再读到同样的帖子和账号不再计费；接着往下刷只算新的那条
        await plugin.tool_browse(source="时间线")
        more = await plugin.tool_browse(source="时间线", more=True)
        assert "第二页的帖子" in more["content"]
        assert plugin._account.spent_usd == pytest.approx(spent_after_login + 3 * 0.005 + 2 * 0.010)

        search = await plugin.tool_browse(source="搜索", query="鲸鱼")
        assert "鲸鱼顶着海带" in search["content"]
        someone = await plugin.tool_browse(source="某人", query="https://x.com/seaside")
        assert "海边的人（@seaside）最近的帖子" in someone["content"] and "海边日落" in someone["content"]
        missing = await plugin.tool_browse(source="帖子", query="https://x.com/a/status/999")
        assert missing["success"] is False and "找不到" in missing["error"]
        replies = await plugin.tool_replies(post="https://twitter.com/catlover/status/101")
        assert "猫猫那条帖子下面的回复" in replies["content"] and "（回复）" in replies["content"]
        status = plugin.status_text()
        assert "@dafeiyu" in status and "这个月：已花 $" in status
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_expired_code_asks_to_login_again(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await plugin.handle_login(stream_id="s1")
        assert "30 秒" in host.sent_texts()[-1]
        state = parse_qs(urlsplit(re.search(r"https?://\S+", host.sent_texts()[-1]).group(0)).query)["state"][0]
        await plugin.handle_auth(stream_id="s1", matched_groups={"url": f"http://127.0.0.1:56130/callback?state={state}&code=stale"})
        assert "过期" in host.sent_texts()[-1] and "/x_login" in host.sent_texts()[-1]
        # 授权码已经交给 X 用掉了：不能再拿同一个链接重试
        await plugin.handle_auth(stream_id="s1", matched_groups={"url": f"http://127.0.0.1:56130/callback?state={state}&code=good-code"})
        assert "没有等待中的授权" in host.sent_texts()[-1]
        await login(plugin, host)
        assert plugin._account.logged_in
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_budget_is_reserved_before_requests(tmp_path, monkeypatch):
    host = FakeHost()
    config = copy.deepcopy(CONFIG)
    config["budget"]["monthly_usd"] = 0.1
    plugin = make_plugin(tmp_path, host, config)

    async def test(world):
        await plugin.on_load()
        await login(plugin, host)
        before = len(world.requests)
        # 10 条 ×（帖子 + 作者）的最坏情况 0.15 美元，超过剩下的预算：不发请求
        result = await plugin.tool_browse(source="时间线")
        assert result["success"] is False and "钱快用完了" in result["error"]
        assert len(world.requests) == before
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_expired_token_is_refreshed_and_rotation_saved(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await login(plugin, host)
        old_refresh = plugin._account.credential()["refresh_token"]
        world.reject_token = world.access  # 当前令牌被 X 判为失效
        result = await plugin.tool_browse(source="时间线")
        assert result["success"], result
        cred = plugin._account.credential()
        assert cred["refresh_token"] != old_refresh and cred["refresh_token"] == world.refresh
        # 续期也被拒：暂停，等主人重新授权；重新授权后自动解除
        world.reject_token = world.access
        world.refresh = "revoked"
        failed = await plugin.tool_browse(source="时间线")
        assert failed["success"] is False and "授权" in failed["error"]
        assert "授权失效" in plugin._account.paused
        await login(plugin, host)
        assert plugin._account.paused == ""
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_rate_limit_cools_down_and_402_pauses_until_resume(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await login(plugin, host)
        world.status = 429
        world.headers = {"x-rate-limit-reset": str(int(time.time()) + 600)}
        limited = await plugin.tool_browse(source="时间线")
        assert limited["success"] is False and "太频繁" in limited["error"]
        world.status = 200
        again = await plugin.tool_browse(source="时间线")
        assert again["success"] is False and "分钟后再来" in again["error"]  # 冷却中不再请求
        await plugin.handle_resume(stream_id="s1")
        assert (await plugin.tool_browse(source="时间线"))["success"]
        spent = plugin._account.spent_usd
        world.status = 402
        broke = await plugin.tool_browse(source="搜索", query="鲸鱼")
        assert broke["success"] is False and "额度用完" in broke["error"]
        assert plugin._account.spent_usd == pytest.approx(spent)  # 出错的请求不计费
        world.status = 200
        restarted = Account(tmp_path / "data" / "account")
        assert "额度用完" in restarted.paused  # 重启也不恢复
        assert "⛔ 已暂停" in plugin.status_text()
        await plugin.handle_resume(stream_id="s1")
        assert "已恢复" in host.sent_texts()[-1]
        assert (await plugin.tool_browse(source="搜索", query="鲸鱼"))["success"]
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_actions_quota_dedupe_follow_and_forbidden(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await login(plugin, host)
        await plugin.tool_browse(source="时间线")
        liked = await plugin.tool_action(post="https://x.com/catlover/status/101", action="点赞")
        assert liked["success"] and "猫猫的帖子" in liked["content"]
        twice = await plugin.tool_action(post="101", action="点赞")
        assert twice["success"] is False and "已经点赞过了" in twice["error"]
        reads = len(world.requests)
        followed = await plugin.tool_action(post="102", action="关注")
        assert followed["success"] and "海边的人（@seaside）" in followed["content"]
        assert len(world.requests) == reads + 1  # 读过的帖子直接知道作者，不再花钱查
        assert world.posts[-1] == {"path": "/2/users/42/following", "target_user_id": "8"}
        assert (await plugin.tool_action(post="@seaside", action="关注"))["error"].endswith("已经关注过了")
        world.action_status = 400
        failed = await plugin.tool_action(post="102", action="点赞")
        assert failed["success"] is False and "没成功" in failed["error"]
        assert plugin._account.today()["actions"]["like"] == 1  # X 明确没做的不占次数
        world.action_status = 403
        denied = await plugin.tool_action(post="102", action="收藏")
        assert denied["success"] is False and "先别用了" in denied["error"]
        world.action_status = 200
        blocked = await plugin.tool_action(post="101", action="收藏")
        assert blocked["success"] is False and "先别用了" in blocked["error"]
        assert (await plugin.tool_browse(source="时间线"))["success"]  # 读照常
        await plugin.handle_resume(stream_id="s1")
        assert (await plugin.tool_action(post="101", action="收藏"))["success"]
        assert "点赞 1" in plugin.status_text()
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_save_emoji_and_disabled(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await login(plugin, host)
        assert (await plugin.tool_save_emoji(image="图1"))["success"] is False
        await plugin.tool_browse(source="时间线")
        saved = await plugin.tool_save_emoji(image="[图1]")
        assert saved["success"] and "一只蓝色的图" in saved["content"]
        data = copy.deepcopy(CONFIG)
        data["plugin"]["enabled"] = False
        plugin.set_plugin_config(data)
        await plugin.on_config_update("self", data, "")
        assert "关着" in (await plugin.tool_browse(source="时间线"))["error"]
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_uncertain_results_are_charged_and_counted(tmp_path, monkeypatch):
    host = FakeHost()
    plugin = make_plugin(tmp_path, host)

    async def test(world):
        await plugin.on_load()
        await login(plugin, host)
        spent = plugin._account.spent_usd
        world.garbage = True
        bad = await plugin.tool_browse(source="时间线")
        assert bad["success"] is False and "看不懂" in bad["error"]
        assert plugin._account.spent_usd == pytest.approx(spent + 10 * 0.015)  # 按预扣的算
        world.garbage = False
        world.drop_action = True
        unsure = await plugin.tool_action(post="101", action="点赞")
        assert unsure["success"] is False and "不知道成没成" in unsure["error"]
        world.drop_action = False
        again = await plugin.tool_action(post="101", action="点赞")
        assert again["success"] is False and "已经点赞过了" in again["error"]  # 不重复做
        assert plugin._account.today()["actions"]["like"] == 1
        await plugin.on_unload()

    with_x(monkeypatch, test)


def test_account_settle_and_link_parsing(tmp_path):
    account = Account(tmp_path, prices=Prices(monthly=1.0))
    held = account.reserve(0.3)
    account.settle(held, lost=True)  # 发出去没拿到结果：按预扣的算
    assert account.spent_usd == pytest.approx(0.3)
    held = account.reserve(0.3)
    try:
        account.reserve(0.5)  # 0.3 已花 + 0.3 预扣中 + 0.5 > 1
        raise AssertionError("超预算应该被拦")
    except QuotaExceeded:
        pass
    account.settle(held, posts=["1", "2", "1"], users=["9"])
    assert account.spent_usd == pytest.approx(0.3 + 2 * 0.005 + 0.010)
    account.reserve(0.2)  # 进程在结算前退出：重启后按预扣的算
    assert Account(tmp_path).spent_usd == pytest.approx(0.3 + 0.02 + 0.2)
    assert post_id_of("https://x.com/someone/status/1850000000000000001?s=20") == "1850000000000000001"
    assert post_id_of("看看这个 https://twitter.com/i/web/status/123456789") == "123456789"
    assert post_id_of("abc") == ""
    assert username_of("@seaside") == "seaside" and username_of("https://x.com/seaside?lang=ja") == "seaside"
    assert username_of("https://x.com/home") == "" and username_of("不是用户名") == ""
