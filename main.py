"""用户档案系统（astrbot_plugin_qq_profile）。

在 QQ 内管理用户档案：绑定账号 → 绑定网站 → 查询域名/备案/证书 → 评价 → 排行。

数据全部存放在 AstrBot 插件维度的 KV 存储里，键名如下（v1.x 保持一致，升级不丢数据）：
    profile_{QQ}          JSON 字符串，档案本体
    _domain_{域名}        QQ 号，域名独占锁
    _user_group_{QQ}      group_id，私聊时推断"上次所在群"
    _next_profile_id      自增编号
    _rankings             JSON 数组，评价榜单

v1.6.0 主要变更（相对 v1.4.1）：
    * 域名归一化（小写、去 www、拒绝 IP 字面量与内网域名），修复独占锁可被绕过
    * 评价体系重做：6 个维度加权 + 动态分母 + 覆盖率收敛，解决"人人 4.8 分"
    * 评价 v1.6.0：维度重构为 6 项可测信号（新增内容与工程），冷启动 3 次采样取中位，
      证书校验 SAN 覆盖，识别 200 伪装的错误页，并记录历史做趋势对比
    * 域名/证书/HTTP 并发探测 + 结果缓存，避免串行 40 秒和第三方限流
    * 免密钥 SSL 直连兜底、SSRF 防护、异常进日志、文案与实现对齐
"""

import asyncio
import html as _html
import json
import re
import socket
import ssl as _ssl
import time
from datetime import datetime, timezone
from ipaddress import ip_address

import aiohttp
from aiohttp import ClientTimeout

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star

SITE_TYPES = ("博客", "企业", "个人", "商城", "论坛", "其他")

# 只接受「标签.一级域」形式的纯主机名；IP 字面量在 normalize_domain 里单独拒绝
RE_DOMAIN = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)

# 不允许绑定的内部/保留后缀，避免机器人被当成内网探测器
BLOCKED_SUFFIXES = (
    ".local", ".localhost", ".internal", ".intranet", ".lan", ".home",
    ".corp", ".test", ".example", ".invalid", ".arpa",
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CACHE_TTL = 600          # 域名探测结果缓存秒数
CACHE_MAX = 256          # 缓存条目上限
HTTP_SAMPLES = 3         # 响应速度采样次数（取中位数，单次测量抖动太大）

# 评价维度：(内部键, 展示名, 权重)。权重合计 100。
# 域名年龄只占 5 分：新域名不该被一棍子打死，它也不是"网站质量"的主要因素。
DIMENSIONS = (
    ("availability", "可用性", 25),
    ("content_engineering", "内容与工程", 25),
    ("ssl", "SSL 证书", 20),
    ("domain_expiry", "域名有效期", 15),
    ("latency", "响应速度", 10),
    ("domain_age", "域名年龄", 5),
)
TOTAL_WEIGHT = sum(w for _, _, w in DIMENSIONS)

# 这些响应头是「工程是否讲究」的可测证据，命中越多越像正经站点
SECURITY_HEADERS = (
    "Strict-Transport-Security",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Content-Security-Policy",
)

# 疑似错误页 / 维护页 / 停放页的特征词（只在标题或极小的页面里匹配，避免误伤正文）
ERROR_PAGE_HINTS = (
    "网站建设中", "站点建设中", "域名未绑定", "未绑定域名", "站点未绑定",
    "域名已过期", "站点已暂停", "该站点不存在", "页面不存在", "正在维护",
    "404 not found", "403 forbidden", "502 bad gateway", "503 service unavailable",
    "domain not found", "site not found", "under construction", "coming soon",
)

RE_META_DESC = re.compile(r'<meta[^>]+name\s*=\s*["\']description["\']', re.IGNORECASE)
RE_META_VIEWPORT = re.compile(r'<meta[^>]+name\s*=\s*["\']viewport["\']', re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 纯函数工具
# --------------------------------------------------------------------------- #

def normalize_domain(raw: str) -> str | None:
    """把用户输入的域名规范化，非法或危险输入返回 None。

    归一化让小写/大写、带不带 www、结尾带不带点 都指向同一个字符串，
    否则 _domain_ 独占锁可以被绕过（Example.com 与 example.com 会当成两个）。
    """
    domain = (raw or "").strip().lower().rstrip(".")
    if not domain:
        return None
    if domain.startswith("www.") and domain.count(".") >= 2:
        domain = domain[4:]
    # 拒绝 127.0.0.1 / 192.168.1.1 / ::1 这类 IP 字面量
    try:
        ip_address(domain)
        return None
    except ValueError:
        pass
    if not RE_DOMAIN.match(domain):
        return None
    if any(domain.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return None
    return domain


def looks_like_error_page(
    status: int | None, title: str | None, html: str
) -> tuple[bool, str | None]:
    """判断页面是不是「伪装成 200 的错误页/维护页/停放页」。

    只在两个地方匹配，避免误伤正常正文：
        * 标题里出现特征词
        * 页面本身很小（< 3KB）且正文里出现特征词
    """
    if status is not None and status >= 400:
        return True, f"HTTP {status}"

    title_text = (title or "").lower()
    for hint in ERROR_PAGE_HINTS:
        if hint.lower() in title_text:
            return True, hint

    if len(html) < 3000:
        head = html[:3000].lower()
        for hint in ERROR_PAGE_HINTS:
            if hint.lower() in head:
                return True, hint
    return False, None


def format_days(days: int | None) -> str:
    """把天数格式化成「x年x个月 / x个月 / x天」。"""
    if days is None:
        return "未知"
    if days >= 365:
        years, rest = days // 365, days % 365
        if rest >= 30:
            return f"{years}年{rest // 30}个月"
        return f"{years}年"
    if days >= 30:
        return f"{days // 30}个月"
    return f"{days}天"


def extract_title(html_text: str) -> str | None:
    """从 HTML 里抠 <title>，解实体（&#8211; 之类），并去掉「站点名 – 副标题」的尾巴。"""
    match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    # 实体不解码的话会得到 "摆烂的小站 &#8211; balareshe" 这种脏标题
    title = _html.unescape(match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    for sep in (" - ", " – ", " — ", " | ", " :: ", " » ", "_", "－"):
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    return title or None


def ratio_level(ratio: float | None) -> str:
    """分数区间 → 图标。"""
    if ratio is None:
        return "➖"
    if ratio >= 0.75:
        return "✅"
    if ratio >= 0.4:
        return "⚪"
    return "🔴"


def bar(ratio: float, width: int = 5) -> str:
    filled = max(0, min(width, round(ratio * width)))
    return "█" * filled + "░" * (width - filled)


def parse_iso_date(value) -> datetime | None:
    """尽力从字符串里解析出 UTC 日期，失败返回 None。"""
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #

class QQProfilePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.plugin_id = "astrbot_plugin_qq_profile"
        config = config or {}

        self.admin_ids: list[str] = []
        raw_admins = config.get("admin_ids", [])
        if isinstance(raw_admins, list):
            self.admin_ids = [str(x).strip() for x in raw_admins if str(x).strip()]

        self.whois_api_key: str = str(config.get("whois_api_key", "") or "").strip()
        self.ssl_api_key: str = str(config.get("ssl_api_key", "") or "").strip()
        self.icp_api_key: str = str(config.get("icp_api_key", "") or "").strip()

        # 自增编号的进程内互斥，避免并发绑定撞号
        self._id_lock = asyncio.Lock()
        # 域名探测结果缓存：{domain: (写入时间戳, info dict)}
        self._cache: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------------ #
    # 数据管理（全局统一存储）
    # ------------------------------------------------------------------ #

    async def _get_profile(self, qq: str) -> dict | None:
        raw = await self.get_kv_data(f"profile_{qq}", None)
        if raw is None:
            return None
        try:
            profile = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            logger.warning(f"[qq_profile] 档案 {qq} 解析失败，已忽略损坏数据")
            return None
        return profile if isinstance(profile, dict) else None

    async def _save_profile(self, qq: str, profile: dict) -> None:
        await self.put_kv_data(f"profile_{qq}", json.dumps(profile, ensure_ascii=False))

    async def _delete_profile(self, qq: str) -> None:
        await self.delete_kv_data(f"profile_{qq}")

    async def _record_last_group(self, qq: str, group_id: str) -> None:
        await self.put_kv_data(f"_user_group_{qq}", group_id)

    async def _get_last_group(self, qq: str) -> str | None:
        raw = await self.get_kv_data(f"_user_group_{qq}", None)
        return str(raw) if raw else None

    async def _get_next_id(self) -> str:
        async with self._id_lock:
            raw = await self.get_kv_data("_next_profile_id", "0")
            current = int(raw) if str(raw).isdigit() else 0
            next_id = current + 1
            await self.put_kv_data("_next_profile_id", str(next_id))
            return str(next_id)

    async def _check_domain_owner(self, domain: str, exclude_qq: str = "") -> dict | None:
        raw = await self.get_kv_data(f"_domain_{domain}", None)
        if raw is None:
            return None
        qq = str(raw)
        if qq == exclude_qq:
            return None
        return await self._get_profile(qq)

    async def _release_domain(self, domain: str, qq: str) -> None:
        """解绑/删除档案时释放域名锁，且不误删别人的锁。"""
        if not domain:
            return
        raw = await self.get_kv_data(f"_domain_{domain}", None)
        if raw is not None and str(raw) == str(qq):
            await self.delete_kv_data(f"_domain_{domain}")

    async def _resolve_group_id(self, event: AstrMessageEvent) -> str | None:
        group_id = event.get_group_id()
        if group_id:
            return group_id
        return await self._get_last_group(event.get_sender_id())

    # ------------------------------------------------------------------ #
    # 权限与事件辅助
    # ------------------------------------------------------------------ #

    async def _get_group_role(self, event: AstrMessageEvent) -> str:
        sender_id = str(event.get_sender_id())
        try:
            group_info = await event.get_group()
            if group_info:
                owner = getattr(group_info, "group_owner", None)
                admins = getattr(group_info, "group_admins", None) or []
                if owner and str(owner) == sender_id:
                    return "群主"
                if sender_id in {str(a) for a in admins}:
                    return "管理员"
                return "成员"
        except Exception as exc:  # 平台不支持取群资料时静默降级
            logger.debug(f"[qq_profile] 获取群身份失败: {exc!r}")
        try:
            if event.is_admin():
                return "管理员"
        except Exception as exc:
            logger.debug(f"[qq_profile] event.is_admin() 不可用: {exc!r}")
        return "成员"

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        """只认插件配置里的 admin_ids 白名单，群主/群管理不算。"""
        return str(event.get_sender_id()) in self.admin_ids

    def _get_at_qq(self, event: AstrMessageEvent) -> str | None:
        # 优先用文档写明的 message_obj.message，回退到 get_messages()
        chain = getattr(getattr(event, "message_obj", None), "message", None)
        if not chain:
            try:
                chain = event.get_messages()
            except Exception:
                chain = None
        for comp in chain or []:
            if isinstance(comp, At) and str(comp.qq) != "all":
                return str(comp.qq)
        return None

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", None)
        if text is None:
            try:
                text = event.get_message_str()
            except Exception:
                text = ""
        return (text or "").strip()

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ------------------------------------------------------------------ #
    # 消息格式化
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_basic_info(
        profile: dict,
        index: int = 1,
        group_role: str | None = None,
        group_id: str | None = None,
    ) -> str:
        pid = profile.get("id", str(index))
        role = group_role or profile.get("groupRole", "未知")
        gid = group_id or profile.get("groupId", "未知")
        return (
            "# 基础信息\n"
            f"编号 '{pid}'\n"
            f"QQ号 '{profile.get('qq', '未知')}'\n"
            f"用户名 '{profile.get('username', '未知')}'\n"
            f"群身份 '{role}'\n"
            f"所在群 '{gid}'"
        )

    @staticmethod
    def _format_site_info(profile: dict) -> str:
        site = profile.get("site")
        if not site:
            return ""
        result = (
            "\n\n# 站点信息\n"
            f"网站名称 '{site.get('name', '')}'\n"
            f"网站类型 '{site.get('type', '')}'\n"
            f"网站域名 '{site.get('domain', '')}'"
        )
        icp = site.get("icp_licence", "")
        if icp:
            result += f"\n备案信息 '{icp}'"
        return result

    # ------------------------------------------------------------------ #
    # 缓存
    # ------------------------------------------------------------------ #

    def _cache_get(self, key: str) -> dict | None:
        item = self._cache.get(key)
        if not item:
            return None
        stamp, value = item
        if time.time() - stamp > CACHE_TTL:
            self._cache.pop(key, None)
            return None
        return value

    def _cache_put(self, key: str, value: dict) -> None:
        if len(self._cache) >= CACHE_MAX:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
        self._cache[key] = (time.time(), value)

    # ------------------------------------------------------------------ #
    # 网络探测
    # ------------------------------------------------------------------ #

    async def _resolve_public_ip(self, domain: str) -> str | None:
        """解析域名，且要求所有解析结果都是公网地址（防 SSRF）。"""
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.run_in_executor(None, socket.getaddrinfo, domain, None),
                timeout=6,
            )
        except Exception as exc:
            logger.info(f"[qq_profile] DNS 解析失败 {domain}: {exc!r}")
            return None

        public: list[str] = []
        for info in infos:
            candidate = info[4][0]
            try:
                addr = ip_address(candidate)
            except ValueError:
                continue
            if (
                addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified
            ):
                logger.warning(
                    f"[qq_profile] 拒绝访问 {domain}：解析到内网地址 {candidate}"
                )
                return None
            public.append(str(addr))
        return public[0] if public else None

    async def _probe_http(self, domain: str) -> dict:
        """抓 HTTP 状态、延迟中位数、Server 头、页面特征与安全响应头。

        除状态码外还采集这些「真实」信号，用于后续打分：
            * 响应速度：取样 HTTP_SAMPLES 次取中位数（单次测量受抖动影响太大）
            * 传输压缩：Content-Encoding 里是否有 gzip/br/zstd
            * 移动适配：<meta name="viewport">
            * 搜索摘要：<meta name="description">
            * 工程规范：HSTS / nosniff / X-Frame-Options / CSP 命中数
            * 错误页伪装：返回 200 但内容像"建设中/未绑定/404"
        """
        result = {
            "status": None, "latency_ms": None, "ttfb_ms": None, "samples": [],
            "server": "", "title": None, "https_ok": False, "scheme": None,
            "reachable": False, "content_encoding": "", "html_bytes": 0,
            "has_meta_description": False, "has_viewport": False,
            "security_headers": 0, "looks_like_error_page": False, "error_hint": None,
        }
        headers = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate, br, zstd"}

        for scheme in ("https", "http"):
            if result["reachable"]:
                break
            url = f"{scheme}://{domain}"
            samples: list[int] = []
            for index in range(HTTP_SAMPLES):
                try:
                    # 每次采样都新建 session：复用连接测出来的是 50ms 级的热连接耗时，
                    # 取中位会把真实首访体验（含 DNS + TLS 握手）掩盖掉。
                    async with aiohttp.ClientSession(
                        timeout=ClientTimeout(total=15), headers=headers
                    ) as session:
                        started = time.perf_counter()
                        async with session.get(url, allow_redirects=True) as resp:
                            if index == 0:
                                ttfb = int((time.perf_counter() - started) * 1000)
                                raw = await resp.content.read(300_000)
                                elapsed = int((time.perf_counter() - started) * 1000)
                                html = raw.decode("utf-8", "ignore")
                                title = extract_title(html)
                                error_page, hint = looks_like_error_page(
                                    resp.status, title, html
                                )
                                result.update(
                                    status=resp.status,
                                    ttfb_ms=ttfb,
                                    server=resp.headers.get("Server", "") or "",
                                    title=title,
                                    scheme=scheme,
                                    reachable=True,
                                    content_encoding=(
                                        resp.headers.get("Content-Encoding") or ""
                                    ).lower(),
                                    html_bytes=len(raw),
                                    has_meta_description=bool(RE_META_DESC.search(html)),
                                    has_viewport=bool(RE_META_VIEWPORT.search(html)),
                                    security_headers=sum(
                                        1 for name in SECURITY_HEADERS
                                        if resp.headers.get(name)
                                    ),
                                    looks_like_error_page=error_page,
                                    error_hint=hint,
                                )
                                if scheme == "https" and 200 <= resp.status < 400:
                                    result["https_ok"] = True
                                samples.append(elapsed)
                            else:
                                await resp.content.read(4096)
                                samples.append(
                                    int((time.perf_counter() - started) * 1000)
                                )
                except Exception as exc:
                    logger.debug(
                        f"[qq_profile] {scheme}://{domain} 第 {index + 1} 次采样失败: {exc!r}"
                    )
                    break
            if samples:
                ordered = sorted(samples)
                result["samples"] = samples
                result["latency_ms"] = ordered[len(ordered) // 2]
        return result

    async def _fetch_geo(self, ip: str) -> str | None:
        try:
            async with aiohttp.ClientSession(
                timeout=ClientTimeout(total=5), headers={"User-Agent": UA}
            ) as session:
                async with session.get(
                    f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=country,regionName,city"
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
            parts = [
                data.get("country", ""),
                data.get("regionName", ""),
                data.get("city", ""),
            ]
            location = " ".join(p for p in parts if p)
            return location or None
        except Exception as exc:
            logger.info(f"[qq_profile] IP 归属地查询失败 {ip}: {exc!r}")
            return None

    async def _fetch_whois_info(self, domain: str) -> dict:
        """自建 WHOIS API。未配置密钥时返回空结果，不影响其他指标。"""
        info: dict = {}
        if not self.whois_api_key:
            return info
        try:
            async with aiohttp.ClientSession(
                timeout=ClientTimeout(total=10), headers={"User-Agent": UA}
            ) as session:
                async with session.get(
                    f"https://whois.blid.top/{domain}",
                    headers={"Authorization": f"Bearer {self.whois_api_key}"},
                ) as resp:
                    if resp.status != 200:
                        logger.info(
                            f"[qq_profile] WHOIS API 返回 {resp.status}（{domain}）"
                        )
                        return info
                    data = await resp.json(content_type=None)
            if not data.get("isRegistered"):
                return info
            info["registrar"] = (data.get("registrar") or {}).get("name")
            dates = data.get("dates") or {}
            for source_key, target_key in (
                ("created", "creation_date"),
                ("expires", "expiration_date"),
                ("updated", "updated_date"),
            ):
                value = dates.get(source_key)
                if value:
                    info[target_key] = str(value)[:10]
            created = parse_iso_date(info.get("creation_date"))
            if created:
                info["domain_age_days"] = max(
                    0, (datetime.now(timezone.utc) - created).days
                )
            expires = parse_iso_date(info.get("expiration_date"))
            if expires:
                info["domain_days_left"] = max(
                    0, (expires - datetime.now(timezone.utc)).days
                )
        except Exception as exc:
            logger.warning(f"[qq_profile] WHOIS 查询异常 {domain}: {exc!r}")
        return info

    async def _fetch_ssl_info(self, domain: str) -> dict:
        """SSL 证书信息：先免密钥直连（还能拿到 issuer / SAN），失败再走 API。"""
        info: dict = {
            "days_left": None, "expiry": None, "not_before": None,
            "issuer": None, "san": [], "source": None, "state": "error",
        }

        # ① 直连 443，免密钥
        try:
            loop = asyncio.get_running_loop()
            pem = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: _ssl.get_server_certificate((domain, 443))
                ),
                timeout=12,
            )
            info["state"] = "ok"
            info["source"] = "直连"
        except asyncio.TimeoutError:
            logger.info(f"[qq_profile] SSL 直连超时 {domain}")
            info["state"] = "timeout"
            pem = None
        except ConnectionRefusedError:
            info["state"] = "refused"
            pem = None
        except Exception as exc:
            logger.info(f"[qq_profile] SSL 直连失败 {domain}: {exc!r}")
            info["state"] = "error"
            pem = None

        if pem:
            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                from cryptography.x509.oid import NameOID

                cert = x509.load_pem_x509_certificate(pem.encode(), default_backend())
                not_after = getattr(cert, "not_valid_after_utc", None)
                if not_after is None:  # cryptography < 42
                    not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
                not_before = getattr(cert, "not_valid_before_utc", None)
                if not_before is None:
                    not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)

                now = datetime.now(timezone.utc)
                info["days_left"] = max(0, (not_after - now).days)
                info["expiry"] = not_after.strftime("%Y-%m-%d")
                info["not_before"] = not_before.strftime("%Y-%m-%d")

                def _attr(name_oid) -> str:
                    attrs = cert.issuer.get_attributes_for_oid(name_oid)
                    return str(attrs[0].value) if attrs else ""

                # 注意：cryptography 里叫 ORGANIZATION_NAME，没有 ORGANIZATION
                info["issuer"] = (
                    _attr(NameOID.ORGANIZATION_NAME) or _attr(NameOID.COMMON_NAME)
                )
                try:
                    san_ext = cert.extensions.get_extension_for_class(
                        x509.SubjectAlternativeName
                    )
                    info["san"] = list(
                        san_ext.value.get_values_for_type(x509.DNSName)
                    )
                except Exception:
                    pass
                info["source"] = "直连"
            except ImportError:
                logger.warning(
                    "[qq_profile] 未安装 cryptography，SSL 直连结果无法解析；"
                    "请在 requirements.txt 里补上 cryptography"
                )
            except Exception as exc:
                logger.warning(f"[qq_profile] 证书解析失败 {domain}: {exc!r}")

        # ② API 兜底
        if info["days_left"] is None and self.ssl_api_key:
            try:
                async with aiohttp.ClientSession(
                    timeout=ClientTimeout(total=10), headers={"User-Agent": UA}
                ) as session:
                    async with session.get(
                        f"https://ssl.blid.top/ssl/{domain}",
                        headers={"Authorization": f"Bearer {self.ssl_api_key}"},
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            current = data.get("current_certificate") or {}
                            days = current.get("days_remaining")
                            if days is not None:
                                info["days_left"] = int(days)
                            issuer = current.get("issuer") or {}
                            info["issuer"] = (
                                issuer.get("common_name")
                                or issuer.get("organization")
                                or info["issuer"]
                            )
                            not_after = current.get("not_after")
                            if not_after:
                                info["not_before"] = current.get("not_before")
                                date = parse_iso_date(not_after)
                                if date:
                                    info["expiry"] = date.strftime("%Y-%m-%d")
                                    if info["days_left"] is None:
                                        info["days_left"] = max(
                                            0,
                                            (date - datetime.now(timezone.utc)).days,
                                        )
                            san = current.get("san") or []
                            if san:
                                info["san"] = list(san)
                            info["source"] = "API"
                            info["state"] = "ok"
                        else:
                            logger.info(
                                f"[qq_profile] SSL API 返回 {resp.status}（{domain}）"
                            )
            except Exception as exc:
                logger.warning(f"[qq_profile] SSL API 异常 {domain}: {exc!r}")

        return info

    async def _fetch_domain_info(self, domain: str) -> dict:
        """并发拿到一个域名的全部实时信息，结果缓存 10 分钟。"""
        cached = self._cache_get(f"domain:{domain}")
        if cached is not None:
            return cached

        ip = await self._resolve_public_ip(domain)
        info: dict = {
            "domain": domain,
            "ip": ip,
            "location": None,
            "status": None,
            "latency_ms": None,
            "ttfb_ms": None,
            "samples": [],
            "server": "",
            "title": None,
            "reachable": False,
            "https_ok": False,
            "content_encoding": "",
            "html_bytes": 0,
            "has_meta_description": False,
            "has_viewport": False,
            "security_headers": 0,
            "looks_like_error_page": False,
            "error_hint": None,
            "registrar": None,
            "creation_date": None,
            "expiration_date": None,
            "updated_date": None,
            "domain_age_days": None,
            "domain_days_left": None,
            "ssl": {"days_left": None, "state": "error", "source": None},
            "error": None,
            "fetched_at": self._now_utc(),
        }

        if ip is None:
            info["error"] = "dns"
            info["ssl"] = await self._fetch_ssl_info(domain)
            self._cache_put(f"domain:{domain}", info)
            return info

        geo_task = asyncio.create_task(self._fetch_geo(ip))
        http_task = asyncio.create_task(self._probe_http(domain))
        ssl_task = asyncio.create_task(self._fetch_ssl_info(domain))
        whois_task = asyncio.create_task(self._fetch_whois_info(domain))

        location, http_result, ssl_result, whois_result = await asyncio.gather(
            geo_task, http_task, ssl_task, whois_task, return_exceptions=True
        )

        def _unwrap(value, label):
            if isinstance(value, BaseException):
                logger.warning(f"[qq_profile] {label} 探测异常: {value!r}")
                return {}
            return value

        location = _unwrap(location, "归属地")
        http_result = _unwrap(http_result, "HTTP")
        ssl_result = _unwrap(ssl_result, "SSL")
        whois_result = _unwrap(whois_result, "WHOIS")

        info["location"] = location or None
        info.update(
            {
                "status": http_result.get("status"),
                "latency_ms": http_result.get("latency_ms"),
                "ttfb_ms": http_result.get("ttfb_ms"),
                "samples": http_result.get("samples") or [],
                "server": http_result.get("server", ""),
                "title": http_result.get("title"),
                "reachable": http_result.get("reachable", False),
                "https_ok": http_result.get("https_ok", False),
                # 下面这些是 v1.6.0 新增的「真实信号」，评价要用
                "content_encoding": http_result.get("content_encoding", ""),
                "html_bytes": http_result.get("html_bytes", 0),
                "has_meta_description": http_result.get("has_meta_description", False),
                "has_viewport": http_result.get("has_viewport", False),
                "security_headers": http_result.get("security_headers", 0),
                "looks_like_error_page": http_result.get("looks_like_error_page", False),
                "error_hint": http_result.get("error_hint"),
            }
        )
        info["ssl"] = ssl_result or {"days_left": None, "state": "error", "source": None}
        info.update(whois_result)

        self._cache_put(f"domain:{domain}", info)
        return info

    # ------------------------------------------------------------------ #
    # 评价算法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dimension_ratios(info: dict) -> list[tuple[str, str, int, float | None, str]]:
        """把原始数据折算成 0~1 的维度比例，返回 (键, 名称, 权重, 比例|None, 说明)。

        比例为 None 表示"这批数据没拿到"，不参与分母，也不扣分。
        """
        rows: list[tuple[str, str, int, float | None, str]] = []
        domain = (info.get("domain") or "").lower()
        status = info.get("status")
        latency = info.get("latency_ms")
        https_ok = bool(info.get("https_ok"))

        # 1. 可用性：状态码 75% + HTTPS 25%；「200 但内容是错误页」再打 4 折
        if not info.get("reachable") or status is None:
            availability = 0.0
            note = "站点无法访问"
        else:
            if 200 <= status < 300:
                base = 1.0
            elif 300 <= status < 400:
                base = 0.9
            elif 400 <= status < 500:
                base = 0.35
            else:
                base = 0.15
            availability = base * 0.75 + (0.25 if https_ok else 0.0)
            if info.get("looks_like_error_page"):
                # 返回 200 但没有真实内容，实质上和打不开差不多
                availability *= 0.25
                note = (
                    f"HTTP {status} · 疑似错误页"
                    f"（{info.get('error_hint') or '内容异常'}）"
                )
            else:
                note = (
                    f"HTTP {status} · {latency}ms · "
                    f"{'HTTPS 正常' if https_ok else '仅 HTTP 可用'}"
                )
        rows.append(("availability", "可用性", 25, availability, note))

        # 2. 内容与工程：6 个可测子项，拿不到的子项不进分母
        if not info.get("reachable"):
            rows.append(("content_engineering", "内容与工程", 25, None, "站点无响应"))
        elif info.get("looks_like_error_page"):
            rows.append((
                "content_engineering", "内容与工程", 25, 0.0,
                "疑似错误页，无实际内容",
            ))
        else:
            subs: list[tuple[int, float | None, str]] = []

            title = info.get("title") or ""
            if title and 2 <= len(title) <= 60 and not info.get("looks_like_error_page"):
                subs.append((6, 1.0, "标题"))
            elif title:
                subs.append((6, 0.6, "标题欠佳"))
            else:
                subs.append((6, 0.0, "无标题"))

            subs.append((5, 1.0 if info.get("has_meta_description") else 0.0,
                         "描述" if info.get("has_meta_description") else "无描述"))
            subs.append((4, 1.0 if info.get("has_viewport") else 0.0,
                         "移动适配" if info.get("has_viewport") else "无 viewport"))

            encoding = (info.get("content_encoding") or "").lower()
            if any(x in encoding for x in ("gzip", "br", "zstd", "deflate")):
                subs.append((4, 1.0, "压缩"))
            elif encoding:
                subs.append((4, 0.3, f"压缩:{encoding}"))
            else:
                # 反向代理可能吃掉这个头，当作无数据，不误判"未压缩"
                subs.append((4, None, "压缩未知"))

            security = info.get("security_headers")
            if security is None:
                subs.append((3, None, "安全头未知"))
            else:
                subs.append((3, min(1.0, security / 3.0), f"安全头{security}/4"))

            size = info.get("html_bytes") or 0
            if size >= 2000:
                subs.append((3, 1.0, "体积正常"))
            elif size >= 1000:
                subs.append((3, 0.6, "体积偏小"))
            elif size > 0:
                subs.append((3, 0.3, "体积过小"))
            else:
                subs.append((3, None, "体积未知"))

            usable = [(w, r) for w, r, _ in subs if r is not None]
            content_ratio = (
                sum(w * r for w, r in usable) / sum(w for w, _ in usable)
                if usable else None
            )
            detail = " · ".join(label for _, r, label in subs if r)
            rows.append((
                "content_engineering", "内容与工程", 25, content_ratio,
                detail or "无可测信号",
            ))

        # 3. 域名有效期
        days_left = info.get("domain_days_left")
        if days_left is None:
            rows.append(("domain_expiry", "域名有效期", 15, None, "未获取到期时间"))
        else:
            if days_left >= 730:
                ratio = 1.0
            elif days_left >= 365:
                ratio = 0.9
            elif days_left >= 180:
                ratio = 0.75
            elif days_left >= 90:
                ratio = 0.55
            elif days_left >= 30:
                ratio = 0.3
            elif days_left >= 7:
                ratio = 0.1
            else:
                ratio = 0.0
            expiry = info.get("expiration_date") or ""
            rows.append(
                ("domain_expiry", "域名有效期", 15, ratio,
                 f"剩余 {format_days(days_left)}" + (f"（{expiry} 到期）" if expiry else ""))
            )

        # 4. SSL 证书：剩余天数 + 证书是否真的覆盖这个域名
        ssl_info = info.get("ssl") or {}
        ssl_days = ssl_info.get("days_left")
        ssl_state = ssl_info.get("state", "error")
        if ssl_days is not None:
            if ssl_days >= 365:
                ratio = 1.0
            elif ssl_days >= 180:
                ratio = 0.95
            elif ssl_days >= 90:
                ratio = 0.85
            elif ssl_days >= 30:
                ratio = 0.6
            elif ssl_days >= 7:
                ratio = 0.3
            elif ssl_days > 0:
                ratio = 0.1
            else:
                ratio = 0.0

            san = [str(x).lower().lstrip("*.") for x in (ssl_info.get("san") or [])]
            covered = (
                not domain
                or not san
                or any(san_name == domain or domain.endswith("." + san_name)
                       for san_name in san)
            )
            if not covered:
                ratio *= 0.6  # 证书存在但不覆盖当前域名，说明配错了

            source = ssl_info.get("source") or "直连"
            note = f"剩余 {format_days(ssl_days)}（{source}）"
            if not covered:
                note += " · 证书未覆盖本域名"
            rows.append(("ssl", "SSL 证书", 20, ratio, note))
        elif ssl_state in ("timeout", "error"):
            # 网络原因导致没测到，不给 0 分，直接不计入分母
            rows.append(("ssl", "SSL 证书", 20, None, "证书检测未完成"))
        else:
            rows.append(("ssl", "SSL 证书", 20, 0.0, "443 端口无可用证书"))

        # 5. 响应速度：冷启动（含 DNS + TLS）多次采样取中位
        if latency is None:
            rows.append(("latency", "响应速度", 10, None, "未获取响应时间"))
        else:
            if latency <= 400:
                ratio = 1.0
            elif latency <= 800:
                ratio = 0.9
            elif latency <= 1200:
                ratio = 0.75
            elif latency <= 2500:
                ratio = 0.5
            elif latency <= 5000:
                ratio = 0.25
            else:
                ratio = 0.1
            samples = info.get("samples") or []
            suffix = f"（冷启动 {len(samples)} 次中位）" if len(samples) > 1 else ""
            rows.append(("latency", "响应速度", 10, ratio, f"{latency}ms{suffix}"))

        # 6. 域名年龄（权重降到 5：新域名不该一棍子打死）
        age_days = info.get("domain_age_days")
        if age_days is None:
            rows.append(("domain_age", "域名年龄", 5, None, "未获取注册时间"))
        else:
            if age_days >= 1825:
                ratio = 1.0
            elif age_days >= 1095:
                ratio = 0.85
            elif age_days >= 365:
                ratio = 0.7
            elif age_days >= 180:
                ratio = 0.5
            elif age_days >= 30:
                ratio = 0.25
            else:
                ratio = 0.1
            rows.append(
                ("domain_age", "域名年龄", 5, ratio, f"已注册 {format_days(age_days)}")
            )

        return rows

    @staticmethod
    def _final_score(rows) -> tuple[float, float]:
        """加权平均 + 覆盖率收敛，返回 (5 分制得分, 数据覆盖率 0~1)。

        覆盖率低时把分数往 3.0 拉，避免"只测到可用性就 5.0 分"的假高分。
        """
        got_weight = sum(w for _, _, w, ratio, _ in rows if ratio is not None)
        if got_weight == 0:
            return 0.0, 0.0
        weighted = sum(w * ratio for _, _, w, ratio, _ in rows if ratio is not None)
        raw = weighted / got_weight
        coverage = got_weight / TOTAL_WEIGHT
        final = raw * coverage + 0.6 * (1 - coverage)
        return round(min(5.0, final * 5), 1), round(coverage, 2)

    @staticmethod
    def _grade_text(score: float) -> str:
        if score >= 4.5:
            return "非常优秀的网站，各方面表现出色。"
        if score >= 3.8:
            return "整体表现良好，可继续打磨细节。"
        if score >= 3.0:
            return "中规中矩，部分指标有提升空间。"
        if score >= 2.0:
            return "存在明显短板，建议检查域名与证书状态。"
        return "网站状态较差，多项指标异常，需要排查。"

    @staticmethod
    def _suggestions(rows, info: dict) -> list[str]:
        """把失分项翻译成「具体能做什么」，最多给 3 条，避免刷屏。"""
        tips: list[str] = []

        if info.get("looks_like_error_page"):
            tips.append(
                f"站点返回 200 但内容像错误页（{info.get('error_hint') or '内容异常'}），"
                "检查是否未绑定域名或处于维护状态"
            )

        ssl_info = info.get("ssl") or {}
        ssl_days = ssl_info.get("days_left")
        if ssl_days is not None and ssl_days <= 30:
            tips.append(f"SSL 证书仅剩 {ssl_days} 天，尽快续签，否则到期后站点会报不安全")
        elif info.get("reachable") and not info.get("https_ok"):
            tips.append("站点未启用 HTTPS 或证书无效，搜索引擎会降权")

        expiry = info.get("domain_days_left")
        if expiry is not None and expiry <= 90:
            tips.append(f"域名 {expiry} 天后到期，记得续费，避免过期后被人抢注")

        if info.get("reachable"):
            if not info.get("has_meta_description"):
                tips.append("缺少 meta description，搜索结果摘要只能由引擎自行截取")
            if not info.get("has_viewport"):
                tips.append("缺少 viewport 声明，手机上会按桌面宽度整体缩放")
            encoding = (info.get("content_encoding") or "").lower()
            if not any(x in encoding for x in ("gzip", "br", "zstd", "deflate")):
                tips.append("未开启 gzip/brotli 压缩，开启后传输体积通常能减半")
            if (info.get("security_headers") or 0) < 2:
                tips.append("响应头缺少 HSTS / nosniff 等安全头，建议补齐")

        return tips[:3]

    # ------------------------------------------------------------------ #
    # 榜单
    # ------------------------------------------------------------------ #

    async def _get_rankings(self) -> list[dict]:
        raw = await self.get_kv_data("_rankings", None)
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except ValueError:
                logger.warning("[qq_profile] 排行榜数据损坏，已重置")
                return []
            return data if isinstance(data, list) else []
        if isinstance(raw, list):
            return raw
        return []

    async def _save_ranking(self, domain: str, name: str, score: float, coverage: float) -> None:
        rankings = await self._get_rankings()
        now = datetime.now(timezone.utc).isoformat()
        for record in rankings:
            if record.get("domain") == domain:
                record.update(score=score, name=name, time=now, coverage=coverage)
                break
        else:
            rankings.append(
                {"domain": domain, "name": name, "score": score,
                 "coverage": coverage, "time": now}
            )
        perfect = [r for r in rankings if r.get("score", 0) >= 5.0]
        perfect.sort(key=lambda r: r.get("time", ""))
        others = [r for r in rankings if r.get("score", 0) < 5.0]
        others.sort(key=lambda r: (-r.get("score", 0), r.get("time", "")))
        await self.put_kv_data(
            "_rankings", json.dumps(perfect[-5:] + others, ensure_ascii=False)
        )

    # ------------------------------------------------------------------ #
    # 评价历史（用于看趋势）
    # ------------------------------------------------------------------ #

    HISTORY_KEEP = 10

    async def _load_history(self, domain: str) -> list[dict]:
        raw = await self.get_kv_data(f"_history_{domain}", None)
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except ValueError:
                logger.warning(f"[qq_profile] {domain} 的评价历史损坏，已重置")
                return []
            return data if isinstance(data, list) else []
        return raw if isinstance(raw, list) else []

    async def _push_history(
        self, domain: str, score: float, coverage: float, rows
    ) -> None:
        history = await self._load_history(domain)
        dims = {
            key: round(ratio, 2)
            for key, _label, _w, ratio, _note in rows
            if ratio is not None
        }
        history.append({
            "t": datetime.now(timezone.utc).isoformat(),
            "score": score,
            "coverage": coverage,
            "dims": dims,
        })
        await self.put_kv_data(
            f"_history_{domain}",
            json.dumps(history[-self.HISTORY_KEEP:], ensure_ascii=False),
        )

    @staticmethod
    def _describe_delta(previous: dict, score: float) -> str | None:
        """把「和上次比」翻译成人话。"""
        old = float(previous.get("score", 0))
        change = round(score - old, 1)
        if abs(change) < 0.05:
            head = f"与上次持平（{old}/5.0）"
        elif change > 0:
            head = f"较上次 ↑{change}（{old} → {score}）"
        else:
            head = f"较上次 ↓{abs(change)}（{old} → {score}）"

        stamp = parse_iso_date(previous.get("t", ""))
        if stamp:
            days = (datetime.now(timezone.utc) - stamp).days
            head += " · " + ("刚刚" if days <= 0 else f"{days} 天前")
        return head

    # ------------------------------------------------------------------ #
    # 命令：帮助
    # ------------------------------------------------------------------ #

    @filter.command("hda")
    async def help_command(self, event: AstrMessageEvent):
        """显示所有命令的说明。"""
        yield event.plain_result(
            "📋 用户档案系统 - 命令列表\n\n"
            "/绑定账号        在群内创建档案\n"
            "/查询档案        查询档案（白名单管理员可 @ 查他人）\n"
            "/绑定网站 域名        智能识别网站标题，类型默认「博客」\n"
            "/绑定网站 名称 类型 域名  手动指定\n"
            "/更新网站 名称 类型 域名  更新站点信息\n"
            "/解绑网站        解绑网站，档案保留\n"
            "/查询域名 [域名]   WHOIS / 存活 / 延迟 / 归属地\n"
            "/查询备案 [域名]   ICP 备案信息\n"
            "/查询证书 [域名]   SSL 证书信息\n"
            "/评价            六维加权评分（5 分制）\n"
            "/排行            评价榜 TOP12\n"
            "/删除档案 @用户   删除档案（仅白名单管理员）\n\n"
            "网站类型：" + "、".join(SITE_TYPES)
        )

    # ------------------------------------------------------------------ #
    # 命令：绑定账号
    # ------------------------------------------------------------------ #

    @filter.command("绑定账号")
    async def bind_account(self, event: AstrMessageEvent):
        """在群内创建用户档案。"""
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
            "qq": sender_id,
            "username": sender_name,
            "groupId": group_id,
            "groupRole": group_role,
            "site": None,
            "createdAt": now,
            "updatedAt": now,
        }
        await self._save_profile(sender_id, new_profile)
        await self._record_last_group(sender_id, group_id)

        result = "✅ 账号绑定成功！\n\n"
        result += self._format_basic_info(new_profile, 1)
        yield event.plain_result(result)

    # ------------------------------------------------------------------ #
    # 命令：查询档案
    # ------------------------------------------------------------------ #

    @filter.command("查询档案")
    async def query_profile(self, event: AstrMessageEvent):
        """查询自己的档案；白名单管理员可以 @ 查他人。"""
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        target_qq = self._get_at_qq(event)
        is_query_other = target_qq is not None and target_qq != sender_id

        if is_query_other:
            if not await self._is_admin(event):
                yield event.plain_result("⛔ 只有白名单管理员可以查询他人的档案。")
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
                    yield event.plain_result(
                        "❌ 未找到您的档案，请先使用 `/绑定账号` 创建档案。"
                    )
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
            yield event.plain_result(
                "❌ 未找到您的档案，请先在群内使用 `/绑定账号` 创建档案。"
            )
            return
        result = self._format_basic_info(profile, 1)
        result += self._format_site_info(profile)
        yield event.plain_result(result)

    # ------------------------------------------------------------------ #
    # 命令：绑定网站
    # ------------------------------------------------------------------ #

    @filter.command("绑定网站")
    async def bind_site(self, event: AstrMessageEvent):
        """绑定网站：/绑定网站 域名 或 /绑定网站 名称 类型 域名。"""
        group_id = await self._resolve_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 未找到您的档案，请先在群内使用 `/绑定账号` 创建档案。")
            return
        if event.get_group_id():
            await self._record_last_group(event.get_sender_id(), group_id)

        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name() or "未知用户"
        args = self._message_text(event).split()[1:]
        argc = len(args)

        if argc == 1:
            domain = normalize_domain(args[0])
            if not domain:
                yield event.plain_result(
                    "❌ 域名格式不正确（不支持 IP 地址、内网域名），请重新输入，如 example.com。"
                )
                return
            name = None  # 智能模式，稍后再抓标题
            site_type = "博客"
        elif argc == 3:
            name, site_type, raw_domain = args[0], args[1], args[2]
            domain = normalize_domain(raw_domain)
            if not name or len(name) > 30:
                yield event.plain_result("❌ 网站名称不能为空且长度不能超过30个字符。")
                return
            if site_type not in SITE_TYPES:
                yield event.plain_result(f"❌ 网站类型只支持：{'、'.join(SITE_TYPES)}")
                return
            if not domain:
                yield event.plain_result(
                    "❌ 域名格式不正确（不支持 IP 地址、内网域名），如 example.com。"
                )
                return
        else:
            yield event.plain_result(
                "❌ 格式错误。支持两种用法：\n\n"
                "① 智能识别：/绑定网站 域名\n"
                "   自动获取网站标题作为名称，类型默认为「博客」\n"
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

        owner = await self._check_domain_owner(domain, sender_id)
        if owner:
            yield event.plain_result(
                f"❌ 域名 {domain} 已被用户 [{owner.get('username', '未知')}] 绑定，请先解绑。"
            )
            return

        # 本地校验都通过后才发起网络请求，避免白跑一次探测
        if not name:
            yield event.plain_result("🔍 正在获取网站信息，请稍候...")
            info = await self._fetch_domain_info(domain)
            name = info.get("title")
            if not name:
                yield event.plain_result(
                    "❌ 无法自动获取网站名称，请手动指定：\n"
                    "/绑定网站 网站名称 网站类型 域名\n\n"
                    "示例：/绑定网站 我的站点 博客 example.com"
                )
                return

        profile["site"] = {"name": name, "type": site_type, "domain": domain}
        profile["updatedAt"] = self._now_utc()
        profile["username"] = sender_name
        if event.get_group_id():
            profile["groupRole"] = await self._get_group_role(event)
            profile["groupId"] = event.get_group_id()

        await self._save_profile(sender_id, profile)
        await self.put_kv_data(f"_domain_{domain}", sender_id)
        await self._record_last_group(sender_id, group_id)

        result = "✅ 站点绑定成功，域名已锁定！\n\n"
        result += self._format_basic_info(profile, 1)
        result += self._format_site_info(profile)
        yield event.plain_result(result)

    # ------------------------------------------------------------------ #
    # 命令：更新网站
    # ------------------------------------------------------------------ #

    @filter.command("更新网站")
    async def update_site(self, event: AstrMessageEvent):
        """更新已绑定网站的名称、类型或域名。"""
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

        parts = self._message_text(event).split(maxsplit=3)
        if len(parts) < 4:
            yield event.plain_result(
                "❌ 格式错误。请使用：\n/更新网站 网站名称 网站类型 网站域名"
            )
            return

        name, site_type, raw_domain = parts[1], parts[2], parts[3]
        domain = normalize_domain(raw_domain)
        if not name or len(name) > 30:
            yield event.plain_result("❌ 网站名称不能为空且长度不能超过30个字符。")
            return
        if site_type not in SITE_TYPES:
            yield event.plain_result(f"❌ 网站类型只支持：{'、'.join(SITE_TYPES)}")
            return
        if not domain:
            yield event.plain_result("❌ 域名格式不正确（不支持 IP 地址、内网域名）。")
            return

        old_domain = (profile.get("site") or {}).get("domain", "")
        if domain != old_domain:
            owner = await self._check_domain_owner(domain, sender_id)
            if owner:
                yield event.plain_result(
                    f"❌ 域名 {domain} 已被用户 [{owner.get('username', '未知')}] 绑定，请先解绑。"
                )
                return

        profile["site"] = {"name": name, "type": site_type, "domain": domain}
        profile["updatedAt"] = self._now_utc()
        await self._save_profile(sender_id, profile)
        await self.put_kv_data(f"_domain_{domain}", sender_id)
        if old_domain and old_domain != domain:
            await self._release_domain(old_domain, sender_id)
        await self._record_last_group(sender_id, group_id)

        yield event.plain_result("✅ 站点信息已更新！")

    # ------------------------------------------------------------------ #
    # 命令：解绑网站
    # ------------------------------------------------------------------ #

    @filter.command("解绑网站")
    async def unbind_site(self, event: AstrMessageEvent):
        """解绑网站，基础档案保留。"""
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

        old_domain = (profile.get("site") or {}).get("domain", "")
        profile["site"] = None
        profile["updatedAt"] = self._now_utc()
        await self._save_profile(sender_id, profile)
        await self._release_domain(old_domain, sender_id)
        await self._record_last_group(sender_id, group_id)

        yield event.plain_result("✅ 已解绑网站，基础档案仍保留。")

    # ------------------------------------------------------------------ #
    # 命令：查询域名
    # ------------------------------------------------------------------ #

    async def _resolve_target_domain(self, event: AstrMessageEvent) -> tuple[str | None, dict | None, bool]:
        """解析命令参数里的域名；没有参数就取用户已绑定域名。

        返回 (域名, 档案, 是否直接指定)。
        """
        parts = re.split(r"\s+", self._message_text(event))
        if len(parts) > 1:
            domain = normalize_domain(parts[1])
            if domain:
                return domain, None, True
        profile = await self._get_profile(event.get_sender_id())
        if profile and (profile.get("site") or {}).get("domain"):
            return profile["site"]["domain"], profile, False
        return None, profile, False

    @filter.command("查询域名")
    async def query_domain(self, event: AstrMessageEvent):
        """查询域名实时信息（WHOIS / 存活 / 延迟 / 归属地）。"""
        sender_id = event.get_sender_id()
        if event.get_group_id():
            await self._record_last_group(sender_id, event.get_group_id())

        domain, profile, direct = await self._resolve_target_domain(event)
        if not domain:
            yield event.plain_result(
                "❌ 您尚未绑定网站，也无法识别域名参数。\n用法：/查询域名 或 /查询域名 域名"
            )
            return

        yield event.plain_result(f"🔍 正在查询 {domain} 的信息，请稍候...")
        info = await self._fetch_domain_info(domain)

        if info.get("error") == "dns":
            yield event.plain_result(
                f"❌ 域名 {domain} 解析失败或指向内网地址，无法查询。"
            )
            return

        if direct:
            lines = ["📋 域名信息", f"域名 '{domain}'"]
        else:
            site = (profile or {}).get("site") or {}
            icp = site.get("icp_licence")
            lines = [
                "📋 域名信息",
                f"网站名称 '{site.get('name', '')}'",
                f"网站类型 '{site.get('type', '')}'",
                f"域名 '{domain}'",
            ]
            if icp:
                lines.append(f"备案信息 '{icp}'")

        if info.get("title"):
            lines.append(f"网站标题 '{info['title']}'")
        if info.get("status") is not None:
            lines.append(f"响应状态 '{info['status']}'")
        if info.get("latency_ms") is not None:
            lines.append(f"响应延迟 '{info['latency_ms']}ms'")
        if info.get("https_ok"):
            lines.append("HTTPS '可用（证书有效）'")
        if info.get("server"):
            lines.append(f"服务器 '{info['server']}'")
        if info.get("ip"):
            lines.append(f"IP '{info['ip']}'")
        if info.get("location"):
            lines.append(f"服务器地区 '{info['location']}'")
        if info.get("registrar"):
            lines.append(f"注册商 '{info['registrar']}'")
        if info.get("creation_date"):
            lines.append(f"注册时间 '{info['creation_date']}'")
        if info.get("expiration_date"):
            lines.append(f"过期时间 '{info['expiration_date']}'")
        if info.get("domain_days_left") is not None:
            lines.append(f"域名有效期 '{format_days(info['domain_days_left'])}'")
        if info.get("updated_date"):
            lines.append(f"最近更新 '{info['updated_date']}'")
        ssl_info = info.get("ssl") or {}
        if ssl_info.get("days_left") is not None:
            lines.append(f"证书剩余 '{format_days(ssl_info['days_left'])}'")

        if info.get("reachable"):
            features = []
            encoding = (info.get("content_encoding") or "").lower()
            features.append("压缩 " + (encoding or "无"))
            features.append("移动适配 " + ("有" if info.get("has_viewport") else "无"))
            features.append(f"安全头 {info.get('security_headers', 0)}/4")
            if info.get("looks_like_error_page"):
                features.append(f"疑似错误页（{info.get('error_hint') or '内容异常'}）")
            lines.append("页面特征 '" + " · ".join(features) + "'")

        if not info.get("reachable"):
            lines.append("⚠️ 站点当前无法访问，以上部分信息可能缺失")

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 命令：查询备案
    # ------------------------------------------------------------------ #

    @filter.command("查询备案")
    async def query_icp(self, event: AstrMessageEvent):
        """查询 ICP 备案信息。"""
        domain, profile, direct = await self._resolve_target_domain(event)
        if not domain:
            yield event.plain_result(
                "❌ 您尚未绑定网站，也无法识别域名参数。\n用法：/查询备案 或 /查询备案 域名"
            )
            return
        if not self.icp_api_key:
            yield event.plain_result("❌ 未配置 ICP 密钥，请在插件配置中填写 ICP密钥。")
            return

        yield event.plain_result(f"🔍 正在查询 {domain} 的 ICP 备案信息，请稍候...")
        try:
            async with aiohttp.ClientSession(
                timeout=ClientTimeout(total=10), headers={"User-Agent": UA}
            ) as session:
                async with session.get(
                    "https://icp.blid.top/query/web",
                    params={"search": domain},
                    headers={"Authorization": f"Bearer {self.icp_api_key}"},
                ) as resp:
                    if resp.status != 200:
                        yield event.plain_result(f"❌ ICP 查询失败，HTTP {resp.status}。")
                        return
                    raw = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning(f"[qq_profile] ICP 查询异常 {domain}: {exc!r}")
            yield event.plain_result(f"❌ ICP 查询失败: {type(exc).__name__}")
            return

        # 实测：文档写返回裸数组，但线上实际返回 {code, msg, params:{list,total}, success}
        # 两种形状都兼容，避免接口哪天改回去就整条链路失效
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            if raw.get("success") is False:
                yield event.plain_result(
                    f"❌ ICP 查询失败：{raw.get('msg') or '接口返回 success=false'}"
                )
                return
            params = raw.get("params") or {}
            items = params.get("list") or []
        else:
            yield event.plain_result("❌ ICP 接口返回了无法识别的内容。")
            return

        # 实测发现 search 参数目前不可靠（换个关键词会返回同一条无关记录），
        # 所以必须自己校验返回的域名与查询域名是否真的相关，否则会张冠李戴。
        def related(item_domain: str) -> bool:
            item_domain = (item_domain or "").strip().lower().rstrip(".")
            if not item_domain:
                return False
            if item_domain == domain:
                return True
            return item_domain.endswith("." + domain) or domain.endswith("." + item_domain)

        matched = [item for item in items if related(item.get("domain", ""))]
        dropped = len(items) - len(matched)
        if dropped:
            logger.warning(
                f"[qq_profile] ICP 返回了 {dropped} 条与 {domain} 无关的记录，已过滤"
            )
        if not matched:
            message = f"📋 未找到 {domain} 的 ICP 备案信息。"
            if dropped:
                message += f"\n（接口返回了 {dropped} 条与当前域名无关的记录，已忽略）"
            yield event.plain_result(message)
            return

        lines = ["📋 ICP 备案信息", ""]
        for index, item in enumerate(matched[:5]):
            if index > 0:
                lines.append("")
            nature = item.get("natureName", "")
            unit_label = "备案主体" if nature == "个人" else "主办单位"
            lines.append(f"域名: {item.get('domain', 'N/A')}")
            lines.append(f"{unit_label}: {item.get('unitName', 'N/A')}")
            lines.append(f"备案号: {item.get('serviceLicence', 'N/A')}")
            lines.append(f"备案性质: {nature}")
            lines.append(f"通过时间: {item.get('updateRecordTime', 'N/A')}")
        if len(matched) > 5:
            lines.append(f"\n... 共 {len(matched)} 条，仅展示前 5 条。")
        if dropped:
            lines.append(f"\n（接口返回 {len(items)} 条，其中 {dropped} 条与当前域名无关已过滤）")

        # 顺手把备案号写回档案，方便 /查询域名 展示
        if not direct and profile:
            icp_no = matched[0].get("serviceLicence", "")
            site = profile.get("site") or {}
            if icp_no and site.get("icp_licence") != icp_no:
                site["icp_licence"] = icp_no
                profile["site"] = site
                await self._save_profile(event.get_sender_id(), profile)

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 命令：查询证书
    # ------------------------------------------------------------------ #

    @filter.command("查询证书")
    async def query_ssl(self, event: AstrMessageEvent):
        """查询 SSL 证书；没有 API 密钥时走免密钥直连。"""
        domain, _profile, _direct = await self._resolve_target_domain(event)
        if not domain:
            yield event.plain_result(
                "❌ 您尚未绑定网站，也无法识别域名参数。\n用法：/查询证书 或 /查询证书 域名"
            )
            return

        yield event.plain_result(f"🔍 正在查询 {domain} 的 SSL 证书，请稍候...")
        info = await self._fetch_ssl_info(domain)

        if info.get("days_left") is None and info.get("expiry") is None:
            if info.get("state") == "refused":
                yield event.plain_result(f"📋 {domain} 未启用 HTTPS（443 端口拒绝连接）。")
            else:
                yield event.plain_result(
                    f"❌ 未能读取 {domain} 的证书。\n"
                    "可在插件配置里填写 SSL密钥 走 API 查询。"
                )
            return

        lines = ["📋 SSL 证书信息", "", f"域名: {domain}"]
        if info.get("issuer"):
            lines.append(f"颁发者: {info['issuer']}")
        if info.get("not_before"):
            lines.append(f"生效时间: {info['not_before']}")
        if info.get("expiry"):
            lines.append(f"到期时间: {info['expiry']}")
        if info.get("days_left") is not None:
            lines.append(f"剩余天数: {info['days_left']} 天")
        san = info.get("san") or []
        if san:
            lines.append(f"覆盖域名: {', '.join(san[:5])}")
            if len(san) > 5:
                lines.append(f"  ... 共 {len(san)} 个域名")
        if info.get("source"):
            lines.append(f"数据来源: {info['source']}")
        if info.get("days_left") is not None and info["days_left"] <= 30:
            lines.append("⚠️ 证书即将到期，请及时续签")

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------ #
    # 命令：删除档案
    # ------------------------------------------------------------------ #

    @filter.command("删除档案")
    async def delete_profile(self, event: AstrMessageEvent):
        """删除用户档案（仅白名单管理员）。"""
        if not await self._is_admin(event):
            yield event.plain_result(
                "⛔ 只有白名单管理员可以执行此操作（在插件配置 admin_ids 中填写 QQ 号）。"
            )
            return

        target_qq = self._get_at_qq(event)
        if not target_qq and not event.get_group_id():
            parts = self._message_text(event).split(maxsplit=1)
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

        username = profile.get("username", "未知")
        old_domain = (profile.get("site") or {}).get("domain", "")

        await self._delete_profile(target_qq)
        await self._release_domain(old_domain, target_qq)
        await self.delete_kv_data(f"_user_group_{target_qq}")

        yield event.plain_result(f"✅ 已删除用户 [{username}] 的档案。")

    # ------------------------------------------------------------------ #
    # 命令：评价
    # ------------------------------------------------------------------ #

    @filter.command("评价")
    async def rate_site(self, event: AstrMessageEvent):
        """对已绑定网站做六维加权评分。"""
        profile = await self._get_profile(event.get_sender_id())
        site = (profile or {}).get("site")
        if not site:
            yield event.plain_result("❌ 您尚未绑定网站，无法进行评价。")
            return

        domain = site["domain"]
        yield event.plain_result(f"🔍 正在分析 {domain} 的各项数据，请稍候...")

        try:
            info = await self._fetch_domain_info(domain)
        except Exception as exc:
            logger.warning(f"[qq_profile] 评价数据获取失败 {domain}: {exc!r}")
            yield event.plain_result(f"❌ 获取 {domain} 的数据失败，请稍后再试。")
            return

        if info.get("error") == "dns":
            yield event.plain_result(f"❌ 域名 {domain} 解析失败，无法评价。")
            return

        if not info.get("reachable"):
            yield event.plain_result(
                f"📊 网站评价 · {site.get('name', domain)}\n"
                "⚫ 0.0/5.0\n\n"
                "❌ 站点当前无法访问（DNS 解析正常但 HTTP 无响应）。\n"
                "本次结果不计入排行榜，请稍后重试。"
            )
            return

        rows = self._dimension_ratios(info)
        score, coverage = self._final_score(rows)
        if coverage == 0:
            yield event.plain_result("❌ 数据严重不足，暂时无法给出评价。")
            return

        # 先取上次记录再写入，才能算出本次的变化
        history = await self._load_history(domain)
        previous = history[-1] if history else None

        full = int(score)
        half = 1 if score - full >= 0.3 else 0
        stars = "⭐" * full + ("✨" if half else "") + "☆" * max(0, 5 - full - half)

        header = f"{stars} {score}/5.0    数据覆盖 {int(coverage * 100)}%"
        if previous:
            delta = self._describe_delta(previous, score)
            if delta:
                header += f"    {delta}"
        else:
            header += "    首次评价，已记录基线"

        lines = [
            f"📊 网站评价 · {site.get('name', domain)}",
            header,
            "",
        ]
        weak: list[str] = []
        for _key, label, weight, ratio, note in rows:
            if ratio is None:
                lines.append(f"➖ {label} {weight}%  {'░' * 5}  无数据 · {note}")
                continue
            lines.append(
                f"{ratio_level(ratio)} {label} {weight}%  "
                f"{bar(ratio)}  {note}"
            )
            if ratio < 0.5:
                weak.append(label)

        lines.append("")
        lines.append(self._grade_text(score))
        if weak:
            lines.append("待改进：" + "、".join(weak))

        tips = self._suggestions(rows, info)
        if tips:
            lines.append("")
            lines.append("建议：")
            for index, tip in enumerate(tips, 1):
                lines.append(f"{index}. {tip}")

        if coverage < 0.75:
            lines.append("")
            lines.append(
                f"注：仅取到 {int(coverage * 100)}% 的指标，分数已按覆盖率向 3.0 收敛，"
                "配置 WHOIS 密钥可获得更准确的评分。"
            )

        yield event.plain_result("\n".join(lines))
        await self._save_ranking(domain, site.get("name", domain), score, coverage)
        await self._push_history(domain, score, coverage, rows)

    # ------------------------------------------------------------------ #
    # 命令：排行
    # ------------------------------------------------------------------ #

    @filter.command("排行")
    async def ranking(self, event: AstrMessageEvent):
        """查看评价排行榜 TOP12。"""
        rankings = await self._get_rankings()
        if not rankings:
            yield event.plain_result("📊 暂无排行榜数据，快去用 /评价 打分吧！")
            return

        perfect = [r for r in rankings if r.get("score", 0) >= 5.0]
        perfect.sort(key=lambda r: r.get("time", ""))
        others = [r for r in rankings if r.get("score", 0) < 5.0]
        others.sort(key=lambda r: (-r.get("score", 0), r.get("time", "")))
        merged = (perfect[-5:] + others)[:12]

        lines = ["🏆 网站排行榜 TOP12"]
        for index, record in enumerate(merged):
            medal = ["🥇", "🥈", "🥉"][index] if index < 3 else f"{index + 1}."
            lines.append(
                f"{medal} {float(record.get('score', 0)):.1f}/5.0  "
                f"{record.get('domain', '')}"
            )
        yield event.plain_result("\n".join(lines))

