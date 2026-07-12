import asyncio
import json
import re
from datetime import datetime, timezone

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.message_components import At

SITE_TYPES = {"博客", "企业", "个人", "商城", "论坛", "其他"}

DOMAIN_REGEX = r"^[a-zA-Z0-9][-a-zA-Z0-9]*(\.[a-zA-Z0-9][-a-zA-Z0-9]*)+$"


class QQProfilePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_id = "astrbot_plugin_qq_profile"
        self.admin_ids: list[str] = []
        if config:
            try:
                raw = config.get("admin_ids", [])
                if isinstance(raw, list):
                    self.admin_ids = [
                        str(x).strip() for x in raw if str(x).strip()
                    ]
            except Exception:
                self.admin_ids = []

    # 数据管理（全局统一存储）

    async def _get_profile(self, qq: str) -> dict | None:
        raw = await self.get_kv_data(f"profile_{qq}", None)
        if raw is None:
            return None
        profile = json.loads(raw) if isinstance(raw, str) else raw
        return profile if isinstance(profile, dict) else None

    async def _save_profile(self, qq: str, profile: dict) -> None:
        await self.put_kv_data(f"profile_{qq}", json.dumps(profile, ensure_ascii=False))

    async def _delete_profile(self, qq: str) -> None:
        await self.delete_kv_data(f"profile_{qq}")

    async def _record_last_group(self, qq: str, group_id: str) -> None:
        await self.put_kv_data(f"_user_group_{qq}", group_id)

    async def _get_next_id(self) -> str:
        key = "_next_profile_id"
        raw = await self.get_kv_data(key, "0")
        current = int(raw) if str(raw).isdigit() else 0
        next_id = current + 1
        await self.put_kv_data(key, str(next_id))
        return str(next_id)

    async def _get_last_group(self, qq: str) -> str | None:
        raw = await self.get_kv_data(f"_user_group_{qq}", None)
        return str(raw) if raw else None

    async def _resolve_group_id(self, event: AstrMessageEvent) -> str | None:
        gid = event.get_group_id()
        if gid:
            return gid
        return await self._get_last_group(event.get_sender_id())

    # 权限 & 辅助

    async def _get_group_role(self, event: AstrMessageEvent) -> str:
        sender_id = event.get_sender_id()
        try:
            group_info = await event.get_group()
            if group_info:
                sid = str(sender_id)
                if group_info.group_owner and str(group_info.group_owner) == sid:
                    return "群主"
                if group_info.group_admins and sid in [str(a) for a in group_info.group_admins]:
                    return "管理员"
                return "成员"
        except Exception:
            pass
        if event.is_admin():
            return "管理员"
        return "成员"

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        return event.get_sender_id() in self.admin_ids

    def _get_at_qq(self, event: AstrMessageEvent) -> str | None:
        for comp in event.get_messages():
            if isinstance(comp, At) and str(comp.qq) != "all":
                return str(comp.qq)
        return None

    # 消息格式化

    @staticmethod
    def _format_basic_info(
        profile: dict, index: int,
        group_role: str | None = None,
        group_id: str | None = None,
    ) -> str:
        pid = profile.get("id", str(index))
        return (
            "# 基础信息\n"
            f"编号 '{pid}'\n"
            f"QQ号 '{profile['qq']}'\n"
            f"用户名 '{profile['username']}'\n"
            f"群身份 '{group_role or profile['groupRole']}'\n"
            f"所在群 '{group_id or profile['groupId']}'"
        )

    @staticmethod
    def _format_site_info(profile: dict) -> str:
        site = profile.get("site")
        if not site:
            return ""
        return (
            "\n\n# 站点信息\n"
            f"网站名称 '{site['name']}'\n"
            f"网站类型 '{site['type']}'\n"
            f"网站域名 '{site['domain']}'"
        )

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 命令：/hda（帮助）

    @filter.command("hda")
    async def help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "📋 用户档案系统 - 命令列表\n\n"
            "/绑定账号   绑定账号创建档案\n"
            "/查询档案   查询档案(管理可查他人)\n"
            "/绑定网站   绑定网站信息\n"
            "/更新网站   更新网站信息\n"
            "/解绑网站   解绑网站信息\n"
            "/查询域名   查询域名实时信息\n"
            "/删除档案   删除用户档案(管理)\n"
            "/评价       对网站进行综合评价\n\n"
            "💡 /绑定网站 支持智能识别\n"
            "发送 /绑定网站 域名 即可自动获取网站名称"
        )

    # 命令：/查询档案

    @filter.command("查询档案")
    async def query_profile(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        target_qq = self._get_at_qq(event)
        is_query_other = target_qq is not None and target_qq != sender_id

        if is_query_other:
            if sender_id not in self.admin_ids:
                yield event.plain_result("⛔ 只有管理员可以查询他人的档案。")
                return
            query_id = target_qq
        else:
            query_id = sender_id

        if group_id:
            await self._record_last_group(sender_id, group_id)
            profile = await self._get_profile(query_id)
            if not profile:
                if is_query_other:
                    yield event.plain_result("❌ 该用户暂无档案。")
                else:
                    yield event.plain_result("❌ 未找到您的档案，请先使用 `/绑定账号` 创建档案。")
                return

            if is_query_other:
                result = self._format_basic_info(profile, 1)
                result += self._format_site_info(profile)
                yield event.plain_result(result)
                return

            group_role = await self._get_group_role(event)
            result = self._format_basic_info(profile, 1, group_role, group_id)
            result += self._format_site_info(profile)
            yield event.plain_result(result)
            return

        if is_query_other:
            yield event.plain_result("❌ 私聊无法查询他人档案。")
            return

        profile = await self._get_profile(query_id)
        if not profile:
            yield event.plain_result("❌ 未找到您的档案，请先在群内使用 `/绑定账号` 创建档案。")
            return

        result = self._format_basic_info(profile, 1)
        result += self._format_site_info(profile)
        yield event.plain_result(result)

    # 命令：/绑定账号

    @filter.command("绑定账号")
    async def bind_account(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("❌ 绑定账号请在群内使用。")
            return

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or "未知用户"

        existing = await self._get_profile(sender_id)
        if existing:
            existing["groupRole"] = await self._get_group_role(event)
            existing["groupId"] = group_id
            existing["updatedAt"] = self._now_utc()
            await self._save_profile(sender_id, existing)
            await self._record_last_group(sender_id, group_id)
            result = "⚠️ 您已绑定账号，信息已同步到当前群。\n\n"
            result += self._format_basic_info(existing, 1)
            result += self._format_site_info(existing)
            yield event.plain_result(result)
            return

        group_role = await self._get_group_role(event)
        now = self._now_utc()
        new_profile = {
            "id": await self._get_next_id(),
            "qq": sender_id, "username": sender_name,
            "groupId": group_id, "groupRole": group_role,
            "site": None, "createdAt": now, "updatedAt": now,
        }
        await self._save_profile(sender_id, new_profile)
        await self._record_last_group(sender_id, group_id)

        result = "✅ 账号绑定成功！\n\n"
        result += self._format_basic_info(new_profile, 1)
        yield event.plain_result(result)

    async def _fetch_website_title(self, domain: str) -> str | None:
        import aiohttp
        from aiohttp import ClientTimeout

        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}"
            try:
                async with aiohttp.ClientSession(
                    timeout=ClientTimeout(total=8)
                ) as session:
                    async with session.get(
                        url, allow_redirects=True, ssl=False
                    ) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()
                        match = re.search(
                            r"<title[^>]*>(.*?)</title>",
                            html,
                            re.IGNORECASE | re.DOTALL,
                        )
                        if match:
                            title = match.group(1).strip()
                            title = re.sub(r"\s+", " ", title)
                            for sep in (" - ", " – ", " — ", " | ", " :: ", " » "):
                                if sep in title:
                                    title = title.split(sep)[0].strip()
                                    break
                            if title:
                                return title[:30]
            except Exception:
                continue
        return None

    @staticmethod
    def _format_whois_date(d) -> str:
        if isinstance(d, list):
            d = d[0] if d else None
        if d:
            if hasattr(d, "strftime"):
                return d.strftime("%Y-%m-%d")
            return str(d)[:10]
        return "未知"

    async def _fetch_domain_info(self, domain: str) -> dict:
        import aiohttp
        import socket
        from aiohttp import ClientTimeout

        info: dict = {
            "title": None, "server": None, "status": None, "ip": None,
            "location": None, "latency": None,
            "registrar": None, "creation_date": None,
            "expiration_date": None, "updated_date": None,
            "domain_days_left": None,
            "ssl_days_left": None, "ssl_expiry": None,
        }

        try:
            loop = asyncio.get_event_loop()
            addr = await loop.run_in_executor(
                None, socket.getaddrinfo, domain, 80
            )
            if addr:
                ip = addr[0][4][0]
                info["ip"] = ip
                try:
                    async with aiohttp.ClientSession(
                        timeout=ClientTimeout(total=5)
                    ) as loc_session:
                        async with loc_session.get(
                            f"http://ip-api.com/json/{ip}?lang=zh-CN"
                        ) as loc_resp:
                            if loc_resp.status == 200:
                                loc_data = await loc_resp.json()
                                parts = [
                                    loc_data.get("country", ""),
                                    loc_data.get("regionName", ""),
                                    loc_data.get("city", ""),
                                ]
                                info["location"] = " ".join(p for p in parts if p)
                except Exception:
                    pass
        except Exception:
            pass

        merged = {}
        try:
            import whois as whois_mod
            loop = asyncio.get_event_loop()

            tld = domain.split(".")[-1]
            servers = [None]
            if tld == "cn":
                servers += ["whois.cnnic.net.cn", "whois.aliyun.com"]
            else:
                servers += ["whois.verisign-grs.com", "whois.godaddy.com", "whois.ionos.com"]

            for srv in servers:
                try:
                    w = await asyncio.wait_for(
                        loop.run_in_executor(None,
                            lambda s=srv: whois_mod.whois(domain, whois_server=s) if s else whois_mod.whois(domain)),
                        timeout=8,
                    )
                    if w:
                        for attr in ["registrar", "creation_date", "expiration_date", "updated_date"]:
                            val = getattr(w, attr, None)
                            if val and not merged.get(attr):
                                merged[attr] = val
                        if merged.get("registrar") and merged.get("creation_date") and merged.get("expiration_date"):
                            break
                except Exception:
                    continue

            if merged:
                info["registrar"] = merged.get("registrar")
                info["creation_date"] = self._format_whois_date(merged.get("creation_date"))
                info["expiration_date"] = self._format_whois_date(merged.get("expiration_date"))
                info["updated_date"] = self._format_whois_date(merged.get("updated_date"))
                exp = merged.get("expiration_date")
                if isinstance(exp, list):
                    exp = exp[0] if exp else None
                if exp and hasattr(exp, "strftime"):
                    delta = exp - datetime.now(timezone.utc)
                    info["domain_days_left"] = max(0, delta.days)
        except Exception:
            pass

        # uapis.cn API 兜底
        if not merged:
            try:
                async with aiohttp.ClientSession(timeout=ClientTimeout(total=8)) as sess:
                    async with sess.get(
                        f"https://uapis.cn/api/v1/network/whois?domain={domain}&format=json"
                    ) as resp:
                        if resp.status == 200:
                            api_data = await resp.json()
                            wd = api_data.get("whois", {})
                            di = wd.get("domain", {})
                            ri = wd.get("registrar", {})
                            api_m = {}
                            if ri.get("name"):
                                api_m["registrar"] = ri["name"]
                            for k, s in [("creation_date","created_date"),("expiration_date","expiration_date"),("updated_date","updated_date")]:
                                v = di.get(s)
                                if v:
                                    api_m[k] = v
                            if api_m:
                                info["registrar"] = api_m.get("registrar")
                                info["creation_date"] = self._format_whois_date(api_m.get("creation_date"))
                                info["expiration_date"] = self._format_whois_date(api_m.get("expiration_date"))
                                info["updated_date"] = self._format_whois_date(api_m.get("updated_date"))
                                exp = api_m.get("expiration_date")
                                if exp:
                                    try:
                                        ed = datetime.strptime(str(exp)[:10], "%Y-%m-%d")
                                        info["domain_days_left"] = max(0, (ed - datetime.now(timezone.utc)).days)
                                    except Exception:
                                        pass
            except Exception:
                pass

        # whoiscx.com API 兜底
        if not info.get("registrar") or not info.get("creation_date"):
            try:
                async with aiohttp.ClientSession(timeout=ClientTimeout(total=8)) as sess:
                    async with sess.get(
                        f"https://api.whoiscx.com/whois/?domain={domain}"
                    ) as resp:
                        if resp.status == 200:
                            cx = await resp.json()
                            if cx.get("status") == 1:
                                ci = cx.get("data", {}).get("info", {})
                                if ci.get("registrar_name"):
                                    info["registrar"] = ci["registrar_name"]
                                if ci.get("creation_time"):
                                    info["creation_date"] = ci["creation_time"][:10]
                                if ci.get("expiration_time"):
                                    info["expiration_date"] = ci["expiration_time"][:10]
                                if ci.get("valid_days") is not None:
                                    info["domain_days_left"] = ci["valid_days"]
            except Exception:
                pass

        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}"
            try:
                async with aiohttp.ClientSession(
                    timeout=ClientTimeout(total=10)
                ) as session:
                    import time as time_mod
                    t0 = time_mod.time()
                    async with session.get(
                        url, allow_redirects=True, ssl=False
                    ) as resp:
                        info["latency"] = f"{int((time_mod.time() - t0) * 1000)}ms"
                        info["status"] = resp.status
                        info["server"] = resp.headers.get("Server", "") or ""
                        html = await resp.text()
                        match = re.search(
                            r"<title[^>]*>(.*?)</title>",
                            html,
                            re.IGNORECASE | re.DOTALL,
                        )
                        if match:
                            title = match.group(1).strip()
                            title = re.sub(r"\s+", " ", title)
                            for sep in (" - ", " – ", " — ", " | ", " :: ", " » "):
                                if sep in title:
                                    title = title.split(sep)[0].strip()
                                    break
                            info["title"] = title[:50]
                        break
            except Exception:
                continue

        # SSL 证书查询：内置直查 → 免费 API 兜底
        ssl_info = await self._fetch_ssl_cert_info(domain)
        if ssl_info.get("ssl_days_left") is not None:
            info["ssl_days_left"] = ssl_info["ssl_days_left"]
            info["ssl_expiry"] = ssl_info["ssl_expiry"]
        if info["ssl_days_left"] is None:
            from urllib.parse import quote
            for ssl_api in [
                f"https://api.shanhe.kim{quote('/API/SSL证书查询.php')}?url=https://{domain}",
                f"https://cn.apihz.cn/api/wangzhan/sslq.php?id=88888888&key=88888888&url={domain}",
            ]:
                try:
                    async with aiohttp.ClientSession(timeout=ClientTimeout(total=8)) as sess:
                        async with sess.get(ssl_api) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                v = d if "days_remaining" in d else d.get("validity", {})
                                dr = v.get("days_remaining")
                                if dr is not None:
                                    info["ssl_days_left"] = int(dr)
                                    vt = v.get("valid_to", "")[:10]
                                    if vt:
                                        info["ssl_expiry"] = vt
                                    break
                except Exception:
                    continue
        return info

    async def _fetch_ssl_cert_info(self, domain: str) -> dict:
        """直接通过 SSL 握手获取证书信息，不依赖 WHOIS"""
        import ssl
        import socket
        result = {"ssl_days_left": None, "ssl_expiry": None}
        try:
            loop = asyncio.get_event_loop()
            cert = await asyncio.wait_for(
                loop.run_in_executor(None, self._get_ssl_cert, domain),
                timeout=8,
            )
            if cert:
                not_after = cert.get("notAfter")
                if not_after:
                    exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                    days = (exp - datetime.now(timezone.utc)).days
                    result["ssl_days_left"] = max(0, days)
                    result["ssl_expiry"] = exp.strftime("%Y-%m-%d")
        except Exception:
            pass
        return result

    @staticmethod
    def _get_ssl_cert(domain: str) -> dict | None:
        import ssl
        import socket
        try:
            ctx = ssl._create_unverified_context()
            with socket.create_connection((domain, 443), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                    return ssock.getpeercert()
        except Exception:
            return None

    # 命令：/绑定网站

    @filter.command("绑定网站")
    async def bind_site(self, event: AstrMessageEvent):
        group_id = await self._resolve_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 未找到您的档案，请先在群内使用 `/绑定账号` 创建档案。")
            return
        if event.get_group_id():
            await self._record_last_group(event.get_sender_id(), group_id)

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or "未知用户"

        parts = event.get_message_str().strip().split()
        args = parts[1:]
        argc = len(args)

        if argc == 1:
            domain = args[0]
            if not re.match(DOMAIN_REGEX, domain):
                yield event.plain_result("❌ 域名格式不正确，请重新输入（如 example.com）。")
                return

            yield event.plain_result("🔍 正在获取网站信息，请稍候...")

            name = await self._fetch_website_title(domain)
            if not name:
                yield event.plain_result(
                    "❌ 无法自动获取网站名称，请手动指定：\n"
                    "/绑定网站 网站名称 网站类型 域名\n\n"
                    "示例：/绑定网站 我的站点 博客 example.com"
                )
                return

            stype = "博客"

        elif argc == 3:
            name, stype, domain = args[0], args[1], args[2]

            if not name or len(name) > 30:
                yield event.plain_result("❌ 网站名称不能为空且长度不能超过30个字符。")
                return
            if stype not in SITE_TYPES:
                yield event.plain_result(f"❌ 网站类型只支持：{'、'.join(SITE_TYPES)}")
                return
            if not re.match(DOMAIN_REGEX, domain):
                yield event.plain_result("❌ 域名格式不正确，请重新输入（如 example.com）。")
                return

        else:
            yield event.plain_result(
                "❌ 格式错误。支持两种用法：\n\n"
                "① 智能识别：/绑定网站 域名\n"
                "   自动获取网站名称，类型默认为「其他」\n"
                "   示例：/绑定网站 blog.umrc.cn\n\n"
                "② 手动指定：/绑定网站 名称 类型 域名\n"
                f"   网站类型可选：{'、'.join(SITE_TYPES)}\n"
                "   示例：/绑定网站 摆烂的小站 博客 blog.umrc.cn"
            )
            return

        profile = await self._get_profile(sender_id)
        if not profile:
            yield event.plain_result("❌ 您尚未绑定账号，请先使用 `/绑定账号` 创建档案。")
            return
        if profile.get("site"):
            yield event.plain_result("⚠️ 您已绑定站点，如需修改请使用 `/更新网站`。")
            return

        now = self._now_utc()

        profile["site"] = {"name": name, "type": stype, "domain": domain}
        profile["updatedAt"] = now
        profile["username"] = sender_name
        if event.get_group_id():
            profile["groupRole"] = await self._get_group_role(event)
            profile["groupId"] = event.get_group_id()

        await self._save_profile(sender_id, profile)
        await self._record_last_group(sender_id, group_id)

        result = "✅ 站点绑定成功！\n\n"
        result += self._format_basic_info(profile, 1)
        result += self._format_site_info(profile)
        yield event.plain_result(result)

    # 命令：/更新网站

    @filter.command("更新网站")
    async def update_site(self, event: AstrMessageEvent):
        group_id = await self._resolve_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 未找到您的档案，请先在群内使用 `/绑定账号` 创建档案。")
            return
        if event.get_group_id():
            await self._record_last_group(event.get_sender_id(), group_id)

        sender_id = event.get_sender_id()
        profile = await self._get_profile(sender_id)

        if not profile:
            yield event.plain_result("❌ 您尚未绑定账号，请先使用 `/绑定账号` 创建档案。")
            return
        if not profile.get("site"):
            yield event.plain_result("❌ 您尚未绑定网站，请先使用 `/绑定网站`。")
            return

        parts = event.get_message_str().strip().split(maxsplit=3)
        if len(parts) < 4:
            yield event.plain_result(
                "❌ 格式错误。请使用：\n"
                "/更新网站 网站名称 网站类型 网站域名"
            )
            return

        name, stype, domain = parts[1], parts[2], parts[3]

        if not name or len(name) > 30:
            yield event.plain_result("❌ 网站名称不能为空且长度不能超过30个字符。")
            return
        if stype not in SITE_TYPES:
            yield event.plain_result(f"❌ 网站类型只支持：{'、'.join(SITE_TYPES)}")
            return
        if not re.match(DOMAIN_REGEX, domain):
            yield event.plain_result(
                "❌ 域名格式不正确，请重新输入（如 example.com）。"
            )
            return

        profile["site"] = {"name": name, "type": stype, "domain": domain}
        profile["updatedAt"] = self._now_utc()
        await self._save_profile(sender_id, profile)
        await self._record_last_group(sender_id, group_id)

        yield event.plain_result("✅ 站点信息已更新！")

    # 命令：/解绑网站

    @filter.command("解绑网站")
    async def unbind_site(self, event: AstrMessageEvent):
        group_id = await self._resolve_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 未找到您的档案，请先在群内使用 `/绑定账号` 创建档案。")
            return
        if event.get_group_id():
            await self._record_last_group(event.get_sender_id(), group_id)

        sender_id = event.get_sender_id()
        profile = await self._get_profile(sender_id)

        if not profile:
            yield event.plain_result("❌ 您尚未绑定账号，请先使用 `/绑定账号` 创建档案。")
            return
        if not profile.get("site"):
            yield event.plain_result("❌ 您尚未绑定网站。")
            return

        profile["site"] = None
        profile["updatedAt"] = self._now_utc()
        await self._save_profile(sender_id, profile)
        await self._record_last_group(sender_id, group_id)

        yield event.plain_result("✅ 已解绑网站，基础档案仍保留。")

    # 命令：/查询域名

    @filter.command("查询域名")
    async def query_domain(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()

        site = None
        if group_id:
            await self._record_last_group(sender_id, group_id)
            profile = await self._get_profile(sender_id)
        else:
            gid = await self._get_last_group(sender_id)
            if gid:
                profile = await self._get_profile(sender_id)
            else:
                profile = None
        if profile and profile.get("site"):
            site = profile["site"]

        if not site:
            yield event.plain_result("❌ 您尚未绑定网站，无法查询域名信息。")
            return

        domain = site["domain"]
        yield event.plain_result(f"🔍 正在查询域名 {domain} 的信息，请稍候...")

        info = await self._fetch_domain_info(domain)

        result = (
            "📋 域名信息\n"
            f"网站名称 '{site['name']}'\n"
            f"网站类型 '{site['type']}'\n"
            f"域名 '{domain}'\n"
        )
        if info.get("title"):
            result += f"网站标题 '{info['title']}'\n"
        if info.get("status"):
            result += f"响应状态 '{info['status']}'\n"
        if info.get("latency"):
            result += f"响应延迟 '{info['latency']}'\n"
        if info.get("server"):
            result += f"服务器 '{info['server']}'\n"
        if info.get("location"):
            result += f"服务器地区 '{info['location']}'\n"
        if info.get("registrar"):
            result += f"注册商 '{info['registrar']}'\n"
        if info.get("creation_date") and info["creation_date"] != "未知":
            result += f"注册时间 '{info['creation_date']}'\n"
        if info.get("expiration_date") and info["expiration_date"] != "未知":
            result += f"过期时间 '{info['expiration_date']}'\n"
        if info.get("updated_date") and info["updated_date"] != "未知":
            result += f"最近更新 '{info['updated_date']}'"

        yield event.plain_result(result)

    # 命令：/删除档案（群主/管理员专用）

    @filter.command("删除档案")
    async def delete_profile(self, event: AstrMessageEvent):
        group_id = await self._resolve_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 未找到可操作的群，请先在群内使用此命令。")
            return
        if event.get_group_id():
            await self._record_last_group(event.get_sender_id(), group_id)

        if not await self._is_admin(event):
            yield event.plain_result("⛔ 只有群主或管理员可以执行此操作。")
            return

        target_qq = self._get_at_qq(event)
        if not target_qq and not event.get_group_id():
            parts = event.get_message_str().strip().split(maxsplit=1)
            if len(parts) >= 2 and parts[1].strip().isdigit():
                target_qq = parts[1].strip()
        if not target_qq:
            yield event.plain_result(
                "❌ 请指定要删除档案的用户。\n"
                "群聊：/删除档案 @用户名\n"
                "私聊：/删除档案 QQ号"
            )
            return

        profile = await self._get_profile(target_qq)
        if not profile:
            yield event.plain_result("❌ 该用户暂无档案。")
            return

        username = profile["username"]
        await self._delete_profile(target_qq)

        yield event.plain_result(f"✅ 已删除用户 [{username}] 的档案。")

    @filter.command("评价")
    async def rate_site(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        profile = await self._get_profile(sender_id)
        if not profile or not profile.get("site"):
            yield event.plain_result("❌ 您尚未绑定网站，无法进行评价。")
            return

        site = profile["site"]
        domain = site["domain"]
        yield event.plain_result(f"🔍 正在分析 {domain} 的各项数据，请稍候...")

        info = await self._fetch_domain_info(domain)
        score = 0.0
        details = []

        # ① 域名有效期 (0.5分)
        days = info.get("domain_days_left")
        if days is not None:
            if days >= 365:
                score += 0.5
                details.append("✅ 域名有效期充裕(>1年)")
            elif days >= 180:
                score += 0.4
                details.append("✅ 域名有效期较长(>6个月)")
            elif days >= 90:
                score += 0.3
                details.append("⚪ 域名有效期尚可(>3个月)")
            elif days >= 30:
                score += 0.1
                details.append("🔴 域名即将到期(<30天)")
            else:
                details.append("🔴 域名已到期")
        else:
            details.append("⚪ 暂无数据域名有效期")

        # ② 证书情况 (0.5分) — SSL 直查 → API 兜底
        ssl_days = info.get("ssl_days_left")
        if ssl_days is not None:
            if ssl_days >= 90:
                score += 0.5
                details.append(f"✅ 证书有效期充足({ssl_days}天)")
            elif ssl_days >= 30:
                score += 0.3
                details.append(f"⚪ 证书即将到期({ssl_days}天)")
            else:
                score += 0.1
                details.append(f"🔴 证书即将过期({ssl_days}天)")
        else:
            exp_date = info.get("expiration_date")
            if exp_date and exp_date != "未知":
                try:
                    exp = datetime.strptime(exp_date, "%Y-%m-%d")
                    remain = (exp - datetime.now(timezone.utc)).days
                    if remain >= 90:
                        score += 0.5
                        details.append("✅ 证书有效期充足(>3个月)")
                    elif remain >= 30:
                        score += 0.3
                        details.append("⚪ 证书即将到期(<30天)")
                    elif remain >= 0:
                        score += 0.1
                        details.append("🔴 证书即将过期")
                    else:
                        details.append("🔴 证书已过期")
                except Exception:
                    details.append("⚪ 暂无数据证书信息")
            else:
                details.append("⚪ 暂无数据证书信息")

        # ③ 网站响应速度 (0.5分)
        status = info.get("status")
        latency = info.get("latency")
        if status == 200:
            if latency:
                try:
                    ms = int(re.sub(r"\D", "", latency))
                    if ms < 500:
                        score += 0.5
                        details.append(f"✅ 响应速度快({ms}ms)")
                    elif ms < 2000:
                        score += 0.3
                        details.append(f"⚪ 响应速度一般({ms}ms)")
                    else:
                        score += 0.1
                        details.append(f"🔴 响应速度较慢({ms}ms)")
                except Exception:
                    score += 0.3
                    details.append("⚪ 响应状态正常")
            else:
                score += 0.3
                details.append("⚪ 响应状态正常")
        elif status:
            score += 0.1
            details.append(f"🔴 网站返回状态码{status}")
        else:
            details.append("🔴 网站无法访问")

        # ④ 标题质量 (0.5分)
        title = info.get("title")
        if title and len(title) >= 4:
            score += 0.5
            details.append("✅ 网站标题完整有意义")
        elif title:
            score += 0.3
            details.append("⚪ 网站标题较短")
        else:
            details.append("⚪ 暂无数据网站标题")

        # ⑤ 服务器信息 (0.5分)
        server = info.get("server")
        if server:
            score += 0.5
            details.append(f"✅ 服务器信息可见({server})")
        else:
            details.append("⚪ 暂无数据服务器信息")

        # ⑥ 服务器地区 (0.5分)
        location = info.get("location")
        if location:
            score += 0.5
            details.append(f"✅ 服务器地区可查({location})")
        else:
            details.append("⚪ 暂无数据服务器地区")

        # ⑦ 注册商 (0.5分)
        registrar = info.get("registrar")
        if registrar:
            score += 0.5
            details.append(f"✅ 注册商信息可见({registrar})")
        else:
            details.append("⚪ 暂无数据注册商信息")

        # ⑧ 域名年龄 (0.5分)
        create_date = info.get("creation_date")
        if create_date and create_date != "未知":
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(create_date))
            if m:
                created = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - created).days
                if age_days >= 365:
                    score += 0.5
                    details.append(f"✅ 域名注册已超过1年({age_days//365}年)")
                elif age_days >= 180:
                    score += 0.3
                    details.append(f"⚪ 域名注册超过6个月")
                else:
                    score += 0.1
                    details.append(f"⚪ 域名注册时间较短")
            else:
                details.append("⚪ 域名年龄未知")
        else:
            details.append("⚪ 暂无数据域名注册时间")

        # ⑨ SSL证书有效性 (0.5分)
        ssl_d = info.get("ssl_days_left")
        if ssl_d is not None:
            if ssl_d > 20:
                score += 0.5
                details.append(f"✅ SSL证书有效({ssl_d}天)")
            else:
                details.append(f"🔴 SSL证书即将过期({ssl_d}天)")
        else:
            details.append("⚪ 暂无数据SSL证书")

        # ⑩ HTTPS与响应状态 (0.5分)
        if status == 200:
            score += 0.5
            details.append("✅ 支持HTTPS且正常响应")
        elif status:
            score += 0.2
            details.append(f"⚪ 网站可访问(状态码{status})")
        else:
            details.append("🔴 网站无法访问")

        # 去掉真正拿不到数据的项（不影响评分）
        valid_count = 10

        if valid_count > 0:
            max_possible = valid_count * 0.5
            final_score = round(score / max_possible * 5, 1)
            final_score = min(final_score, 5.0)
        else:
            final_score = 0.0

        full = int(final_score)
        half = 1 if final_score - full >= 0.3 else 0
        stars = "⭐" * full + ("✨" if half else "") + "☆" * (5 - full - half)

        if final_score >= 4.5:
            comment = "非常优秀的网站！各方面表现出色，维护状态极佳。"
        elif final_score >= 3.5:
            comment = "整体表现良好，各方面较为均衡，可继续优化细节。"
        elif final_score >= 2.5:
            comment = "中规中矩，部分项目有待加强，建议关注安全和稳定性。"
        elif final_score >= 1.5:
            comment = "存在较多问题，建议尽快检查域名和服务器配置。"
        elif final_score >= 0.5:
            comment = "网站状态较差，多项指标异常，需要全面排查。"
        else:
            comment = "暂无有效数据或网站无法正常访问。"

        pos = [d for d in details if d.startswith("✅")]
        neg = [d for d in details if d.startswith("🔴")]
        summary = f"优势{len(pos)}项" + (f" 待改进{len(neg)}项" if neg else "")

        result = (
            f"📋 网站评价 - {site['name']}\n"
            f"{stars} {final_score}/5.0  ({summary} 基于{valid_count}/10项数据)\n\n"
            f"{comment}\n\n"
            f"详细评分：\n"
        )
        result += "\n".join(details)
        ssl_expiry = info.get("ssl_expiry")
        if ssl_expiry:
            result += f"\n\nSSL证书有效期至 {ssl_expiry}"
        yield event.plain_result(result)
