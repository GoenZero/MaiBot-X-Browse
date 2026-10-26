"""她自己的 X 账号：授权凭证、每月花费、每日配额、出错暂停。

- 授权凭证（access / refresh token）存在插件数据目录的 credential.json（只有插件读），不出现在 WebUI 里，也不给模型看。
- 花费用整数「微美元」记账：请求前按最贵的情况先预扣，拿到结果再按实际读到的条数结算；
  请求发出去却没拿到结果（断网、超时）就按预扣的算（宁可少刷不超支）。思路参考 OpenClaw 的 X 接入。
- X 对同一 UTC 日内重复读到的同一条帖子、同一个账号只收一次钱，这里也照此去重估算。
- 授权失效、X 拒绝访问、开发者额度用完就持久暂停：重启也不恢复，只有主人发 /x_resume（或重新 /x_login）才解除。
  X 说太频繁（429）只是冷却到它给的时间，之后自动恢复。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import json
import os
import time

MICRO = 1_000_000
ACTIONS = ("like", "bookmark", "follow")
ACTION_NAMES = {"like": "点赞", "bookmark": "收藏", "follow": "关注"}
# 每天去重记录最多记多少个 ID
SEEN_KEEP = 5000


@dataclass
class Limits:
    requests_per_day: int = 150
    request_interval: float = 2.0
    action_interval: float = 20.0
    per_day: Dict[str, int] = field(default_factory=lambda: {"like": 20, "bookmark": 10, "follow": 3})


@dataclass
class Prices:
    monthly: float = 10.0
    post_read: float = 0.005
    user_read: float = 0.010
    action: float = 0.015

    def micros(self, usd: float) -> int:
        return int(round(usd * MICRO))


class AccountPaused(Exception):
    """账号操作暂停中（给 AI 看的原因）。"""


class QuotaExceeded(Exception):
    """今天的次数或这个月的花费用完了，或者这条已经做过了。"""


class Account:
    """凭证与守卫的状态都存在数据目录，重启不丢。"""

    def __init__(self, data_dir: Path, limits: Optional[Limits] = None, prices: Optional[Prices] = None) -> None:
        self.dir = data_dir
        self.limits = limits or Limits()
        self.prices = prices or Prices()
        self._state = self._read("account_state.json") or {}
        self._last_request = 0.0
        self._last_action = 0.0
        # 上次退出时还没结算的请求（发出去了，可能已经扣费）：按预扣的算
        if leftover := sum(int(v) for v in (self._state.pop("pending", None) or {}).values()):
            self.month()["spent"] = int(self.month().get("spent", 0)) + leftover
            self._save_state()

    # ── 凭证 ──────────────────────────────────────

    def credential(self) -> Dict[str, Any]:
        return self._read("credential.json") or {}

    @property
    def logged_in(self) -> bool:
        cred = self.credential()
        return bool(cred.get("user_id") and (cred.get("refresh_token") or cred.get("access_token")))

    @property
    def user_id(self) -> str:
        return str(self.credential().get("user_id") or "")

    @property
    def username(self) -> str:
        return str(self.credential().get("username") or "")

    def save_tokens(self, token: Dict[str, Any]) -> None:
        """保存 X 换回来的令牌（refresh token 每次刷新都会换新的，必须马上存下）。"""
        cred = self.credential()
        cred["access_token"] = str(token.get("access_token") or "")
        if token.get("refresh_token"):
            cred["refresh_token"] = str(token["refresh_token"])
        cred["expires_at"] = time.time() + float(token.get("expires_in") or 7200)
        cred["scope"] = str(token.get("scope") or cred.get("scope") or "")
        cred["saved_at"] = time.time()
        self._write("credential.json", cred)

    def save_profile(self, user_id: str, username: str, name: str) -> None:
        cred = self.credential()
        cred.update(user_id=user_id, username=username, name=name)
        self._write("credential.json", cred)
        self.resume(reason_contains="授权")  # 重新授权后，因为授权失效而暂停的状态自然解除

    def forget(self) -> None:
        """开始新的授权时清掉旧凭证（换号或重新授权）。"""
        try:
            (self.dir / "credential.json").unlink()
        except FileNotFoundError:
            pass

    # ── 暂停与冷却 ────────────────────────────────

    @property
    def paused(self) -> str:
        return str(self._state.get("paused_reason") or "")

    def pause(self, reason: str) -> None:
        if not self.paused:
            self._state["paused_reason"] = reason
            self._state["paused_at"] = time.time()
            self._save_state()

    def resume(self, *, reason_contains: str = "") -> bool:
        """解除暂停；主人手动恢复（不带 reason_contains）时也清掉被拒绝的功能和冷却。"""
        changed = False
        if not reason_contains:
            for key in ("blocked", "cooldown_until"):
                if self._state.pop(key, None):
                    changed = True
        if self.paused and (not reason_contains or reason_contains in self.paused):
            self._state.pop("paused_reason", None)
            self._state.pop("paused_at", None)
            changed = True
        if changed:
            self._save_state()
        return changed

    @property
    def cooldown(self) -> int:
        """X 说太频繁后还要等几秒。"""
        return max(0, int(float(self._state.get("cooldown_until") or 0) - time.time()))

    def cool_down(self, until: float) -> None:
        self._state["cooldown_until"] = max(until, time.time() + 60)
        self._save_state()

    def blocked(self, feature: str) -> str:
        return str((self._state.get("blocked") or {}).get(feature) or "")

    def block(self, feature: str, reason: str) -> None:
        self._state.setdefault("blocked", {})[feature] = reason
        self._save_state()

    # ── 花费 ──────────────────────────────────────

    def month(self) -> Dict[str, Any]:
        key = time.strftime("%Y-%m", time.localtime(time.time()))
        month = self._state.get("month")
        if not isinstance(month, dict) or month.get("key") != key:
            month = {"key": key, "spent": 0, "posts": 0, "users": 0, "actions": 0}
            self._state["month"] = month
        return month

    @property
    def spent_usd(self) -> float:
        return int(self.month().get("spent", 0)) / MICRO

    def reserve(self, usd: float) -> str:
        """请求前按最贵的情况预扣（存盘，进程意外退出也不会漏算）；这个月剩下的钱不够就抛 QuotaExceeded。返回结算用的编号。"""
        need = self.prices.micros(usd)
        budget = self.prices.micros(self.prices.monthly)
        pending = self._state.setdefault("pending", {})
        if int(self.month().get("spent", 0)) + sum(int(v) for v in pending.values()) + need > budget:
            raise QuotaExceeded(f"这个月刷 X 的钱快用完了（已花 ${self.spent_usd:.2f} / ${self.prices.monthly:.2f}），下个月再来")
        key = os.urandom(6).hex()
        pending[key] = need
        self._save_state()
        return key

    def settle(self, key: str, *, posts: Iterable[str] = (), users: Iterable[str] = (), actions: int = 0, lost: bool = False) -> int:
        """结算一次请求：lost=True（发出去了但没拿到结果）按预扣的算；否则按实际读到的、今天还没读过的条数算。"""
        reserved = int((self._state.get("pending") or {}).pop(key, 0))
        month = self.month()
        if lost:
            cost = reserved
        else:
            new_posts = self._mark_seen("posts", posts)
            new_users = self._mark_seen("users", users)
            cost = (
                new_posts * self.prices.micros(self.prices.post_read)
                + new_users * self.prices.micros(self.prices.user_read)
                + actions * self.prices.micros(self.prices.action)
            )
            month["posts"] = int(month.get("posts", 0)) + new_posts
            month["users"] = int(month.get("users", 0)) + new_users
            month["actions"] = int(month.get("actions", 0)) + actions
        month["spent"] = int(month.get("spent", 0)) + cost
        self._save_state()
        return cost

    def _mark_seen(self, kind: str, ids: Iterable[str]) -> int:
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time()))  # X 按 UTC 日去重
        seen = self._state.get("seen")
        if not isinstance(seen, dict) or seen.get("date") != today:
            seen = {"date": today, "posts": [], "users": []}
            self._state["seen"] = seen
        known = seen.setdefault(kind, [])
        known_set = set(known)
        new = 0
        for item in ids:
            item = str(item)
            if item and item not in known_set:
                known_set.add(item)
                known.append(item)
                new += 1
        del known[:-SEEN_KEEP]
        return new

    # ── 配额与节奏 ────────────────────────────────

    def check_open(self) -> None:
        """暂停或冷却中就抛异常（每次真正发请求前都要查，等待期间别的请求可能刚出了问题）。"""
        if self.paused:
            raise AccountPaused(f"X 那边暂停了：{self.paused}。等主人处理后再试")
        if wait := self.cooldown:
            raise AccountPaused(f"X 说刷得太频繁了，{max(1, wait // 60)} 分钟后再来")

    def before_request(self) -> float:
        """请求前调用：暂停、冷却或今天请求数用完时抛异常；返回还需要等几秒（调用方去 sleep）。"""
        self.check_open()
        day = self._roll()
        if int(day.get("requests", 0)) >= self.limits.requests_per_day:
            raise QuotaExceeded("今天刷 X 的次数用完了，明天再来")
        wait = max(0.0, self._last_request + self.limits.request_interval - time.monotonic())
        day["requests"] = int(day.get("requests", 0)) + 1
        self._last_request = time.monotonic() + wait
        self._save_state()
        return wait

    def check_action(self, action: str, key: str) -> float:
        """操作前检查：今天次数、这条是否做过。返回还需要等几秒。"""
        if action not in ACTIONS:
            raise QuotaExceeded(f"不支持的操作：{action}")
        if self.paused:
            raise AccountPaused(f"X 那边暂停了：{self.paused}。等主人处理后再试")
        day = self._roll()
        counts = day.setdefault("actions", {})
        if int(counts.get(action, 0)) >= int(self.limits.per_day.get(action, 0)):
            raise QuotaExceeded(f"今天{ACTION_NAMES[action]}的次数用完了")
        if key in self._done(action):
            raise QuotaExceeded(f"这个已经{ACTION_NAMES[action]}过了")
        return max(0.0, self._last_action + self.limits.action_interval - time.monotonic())

    def record_action(self, action: str, key: str) -> None:
        """发送前先记下（中途断网、被取消、进程退出都算做过：宁可少做一次，不能超上限）。"""
        day = self._roll()
        counts = day.setdefault("actions", {})
        counts[action] = int(counts.get(action, 0)) + 1
        done = self._done(action)
        if key not in done:
            done.append(key)
        del done[:-500]  # 只记最近 500 个，够判断重复
        self._last_action = time.monotonic()
        self._save_state()

    def undo_action(self, action: str, key: str) -> None:
        """确定没做成（X 明确拒绝、请求没发出去）时撤回预先记下的操作。"""
        day = self._roll()
        counts = day.setdefault("actions", {})
        counts[action] = max(0, int(counts.get(action, 0)) - 1)
        done = self._done(action)
        if key in done:
            done.remove(key)
        self._save_state()

    def today(self) -> Dict[str, Any]:
        return self._roll()

    # ── 内部 ──────────────────────────────────────

    def _done(self, action: str) -> list:
        return self._state.setdefault("done", {}).setdefault(action, [])

    def _roll(self) -> Dict[str, Any]:
        today = time.strftime("%Y-%m-%d", time.localtime(time.time()))
        day = self._state.get("day")
        if not isinstance(day, dict) or day.get("date") != today:
            day = {"date": today, "requests": 0, "actions": {}}
            self._state["day"] = day
        return day

    def _save_state(self) -> None:
        self._write("account_state.json", self._state)

    def _read(self, name: str) -> Any:
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, name: str, data: Any) -> None:
        """原子写入；文件只有插件自己能读（credential.json 里有授权令牌）。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / name
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(temp, 0o600)
        os.replace(temp, path)
