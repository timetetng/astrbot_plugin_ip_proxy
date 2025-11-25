# main.py
import asyncio
import json
import os
import re
import time
from asyncio import Server, StreamReader, StreamWriter, Task
from datetime import date, datetime

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools, register

# 导入可视化相关库，如果失败则禁用该功能
try:
    import jinja2

    from .visual import render_status_card
    VISUAL_ENABLED = True
except ImportError:
    VISUAL_ENABLED = False


# 使用 @register 装饰器注册插件
@register(
    "astrbot_plugin_ip_proxy",
    "timetetng",
    "一个将HTTP代理API转换为本地代理的AstrBot插件 ",
    "1.6",  # 版本号更新
    "https://github.com/timetetng/astrbot_plugin_ip_proxy"
)
class IPProxyPlugin(Star):
    """
    IP代理插件主类。
    采用 AstrBot 配置系统，通过API获取HTTP代理IP，并在本地启动一个代理服务。
    用户可以通过独立的指令来控制和配置代理服务。
    所有指令均需要管理员权限。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config: AstrBotConfig = config
        self.server: Server | None = None
        self.server_task: Task | None = None
        self.current_ip: str | None = None
        self.current_port: int | None = None
        self.last_validation_time: float | None = None
        self.ip_lock = asyncio.Lock()
        self.stats_lock = asyncio.Lock()
        self.http_session = aiohttp.ClientSession()

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_ip_proxy")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # 定义缓存目录
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.stats_file = self.data_dir / "stats.json"
        self.stats = {}
        self._load_stats_sync()

        logger.info("IP代理插件: 插件已加载，配置已注入。")

        if not VISUAL_ENABLED:
            logger.warning("IP代理插件: jinja2 或 playwright 未安装，可视化状态卡片功能将不可用。")

        if self.config.get("start_on_load", True):
            logger.info("IP代理插件: 根据配置，正在自动启动代理服务...")
            self.server_task = asyncio.create_task(self.start_local_proxy_server())

    # --- 流量格式化辅助函数 ---
    def _format_bytes(self, size: int) -> str:
        """将字节数格式化为可读的KB, MB, GB等"""
        if size < 1024:
            return f"{size} B"
        for unit in ["KB", "MB", "GB", "TB", "PB"]:
            size /= 1024.0
            if size < 1024.0:
                return f"{size:.2f} {unit}"
        return f"{size:.2f} EB"

    # --- [新增] 流量解析辅助函数 ---
    def _parse_traffic_string(self, traffic_str: str) -> int | None:
        """解析流量字符串 (e.g., "5GB", "1000MB") 为字节数"""
        traffic_str = traffic_str.strip().upper()
        match = re.match(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|PB)?", traffic_str)
        if not match:
            return None

        value = float(match.group(1))
        unit = match.group(2)

        if unit == "B" or unit is None:
            return int(value)
        elif unit == "KB":
            return int(value * 1024)
        elif unit == "MB":
            return int(value * 1024**2)
        elif unit == "GB":
            return int(value * 1024**3)
        elif unit == "TB":
            return int(value * 1024**4)
        elif unit == "PB":
            return int(value * 1024**5)
        return None

    # --- 数据持久化核心功能 ---
    async def _check_and_reset_daily_stats(self):
        """检查日期，如果跨天则重置每日统计"""
        today_str = date.today().isoformat()
        async with self.stats_lock:
            if self.stats.get("today_date") != today_str:
                logger.info("日期已更新，重置每日IP与流量统计。")
                # 记录前一天的流量到历史记录
                if self.stats.get("today_traffic_bytes") is not None:
                    if "daily_traffic_history" not in self.stats:
                        self.stats["daily_traffic_history"] = []
                    # 确保历史记录不超过最近3条
                    self.stats["daily_traffic_history"].append(self.stats["today_traffic_bytes"])
                    if len(self.stats["daily_traffic_history"]) > 3:
                        self.stats["daily_traffic_history"].pop(0)

                self.stats["today_date"] = today_str
                self.stats["today_ips_used"] = 0
                self.stats["today_requests_succeeded"] = 0
                self.stats["today_requests_failed"] = 0
                self.stats["today_traffic_bytes"] = 0
                await self._save_stats()

    def _load_stats_sync(self):
        """同步版本的数据加载，用于 __init__"""
        try:
            if self.stats_file.exists():
                with open(self.stats_file, encoding="utf-8") as f:
                    self.stats = json.load(f)
                logger.info("IP代理插件: 成功加载统计数据。")
            else:
                self.stats = {}
        except Exception as e:
            logger.error(f"IP代理插件: 加载统计数据失败: {e}，将使用默认值。")
            self.stats = {}

        self.stats.setdefault("total_ips_used", 0)
        self.stats.setdefault("today_date", "1970-01-01")
        self.stats.setdefault("today_ips_used", 0)
        self.stats.setdefault("today_requests_succeeded", 0)
        self.stats.setdefault("today_requests_failed", 0)
        self.stats.setdefault("total_traffic_bytes", 0)
        self.stats.setdefault("today_traffic_bytes", 0)
        self.stats.setdefault("daily_traffic_history", [])
        self.stats.setdefault("total_traffic_limit_bytes", 0)

    async def _save_stats(self):
        """
        [修改] 保存统计数据到文件，采用原子写入方式防止数据损坏。
        """
        temp_file_path = self.stats_file.with_suffix(".json.tmp")
        try:
            # 1. 将数据写入临时文件
            with open(temp_file_path, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, indent=4)
            # 2. 如果写入成功，则用临时文件替换原文件
            os.replace(temp_file_path, self.stats_file)
        except Exception as e:
            logger.error(f"IP代理插件: 保存统计数据失败: {e}")
            # 如果失败，尝试删除可能残留的临时文件
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

    async def _increment_request_counter(self, success: bool):
        """记录请求成功或失败，现在是异步的"""
        await self._check_and_reset_daily_stats()
        async with self.stats_lock:
            if success:
                self.stats["today_requests_succeeded"] += 1
            else:
                self.stats["today_requests_failed"] += 1
            await self._save_stats()

    async def _increment_ip_usage_counter(self):
        """记录IP使用量，现在是异步的"""
        await self._check_and_reset_daily_stats()
        async with self.stats_lock:
            self.stats["total_ips_used"] += 1
            self.stats["today_ips_used"] += 1
            await self._save_stats()

    async def _forward_and_track(self, src: StreamReader, dst: StreamWriter):
        """
        从源读取数据，写入目标，并实时统计流量。
        """
        try:
            while not src.at_eof():
                data = await src.read(4096)
                if not data: break

                # --- 核心流量统计逻辑 ---
                traffic_this_chunk = len(data)
                async with self.stats_lock:
                    self.stats["total_traffic_bytes"] += traffic_this_chunk
                    self.stats["today_traffic_bytes"] += traffic_this_chunk

                    # 检查总流量限制
                    total_limit = self.stats.get("total_traffic_limit_bytes", 0)
                    if total_limit > 0 and self.stats["total_traffic_bytes"] >= total_limit:
                        logger.warning(f"总流量已达到或超过限制 ({self._format_bytes(total_limit)})，将停止当前转发。")
                        break

                dst.write(data)
                await dst.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            if not dst.is_closing():
                dst.close()

    # 重写 handle_connection，调用新的转发和统计方法
    async def handle_connection(self, reader: StreamReader, writer: StreamWriter):
        addr = writer.get_extra_info("peername")
        remote_writer: StreamWriter | None = None
        is_counted = False

        # 在处理新连接前检查流量限制，如果已达到限制，则拒绝连接
        async with self.stats_lock:
            total_limit = self.stats.get("total_traffic_limit_bytes", 0)
            if total_limit > 0 and self.stats["total_traffic_bytes"] >= total_limit:
                logger.warning(f"总流量已达到限制，拒绝新连接来自 {addr}。")
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
                await writer.drain()
                if not writer.is_closing(): writer.close()
                return

        try:
            initial_data = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if not initial_data: return

            allowed_domains = set(self.config.get("allowed_domains", []))
            if not allowed_domains:
                logger.warning("IP代理插件: 未配置allowed_domains，拒绝所有代理请求。")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            hostname = self._extract_hostname(initial_data)
            if not hostname or hostname not in allowed_domains:
                logger.warning(f"IP代理插件: 拒绝来自 {addr} 的非白名单域名请求: {hostname if hostname else '未知主机'}")
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            logger.debug(f"接受来自 {addr} 的请求，转发到白名单主机: {hostname}")
            remote_ip, remote_port = await self.get_valid_ip()
            if not remote_ip or not remote_port:
                logger.error(f"无法为来自 {addr} 的白名单请求获取有效代理IP。")
                await self._increment_request_counter(success=False); is_counted = True
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            connect_timeout = self.config.get("connect_timeout", 10)
            conn_future = asyncio.open_connection(remote_ip, remote_port)
            remote_reader, remote_writer = await asyncio.wait_for(conn_future, timeout=connect_timeout)

            await self._increment_request_counter(success=True); is_counted = True

            # 将首个数据块的流量也计入统计
            traffic_initial_chunk = len(initial_data)
            async with self.stats_lock:
                self.stats["total_traffic_bytes"] += traffic_initial_chunk
                self.stats["today_traffic_bytes"] += traffic_initial_chunk

                # 再次检查总流量限制
                total_limit = self.stats.get("total_traffic_limit_bytes", 0)
                if total_limit > 0 and self.stats["total_traffic_bytes"] >= total_limit:
                    logger.warning(f"总流量已达到或超过限制 ({self._format_bytes(total_limit)})，关闭当前连接。")
                    writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    if not remote_writer.is_closing(): remote_writer.close()
                    if not writer.is_closing(): writer.close()
                    # 达到流量限制后不再接受新连接，但无需主动停止服务
                    return

            remote_writer.write(initial_data)
            await remote_writer.drain()

            task1 = asyncio.create_task(self._forward_and_track(reader, remote_writer))
            task2 = asyncio.create_task(self._forward_and_track(remote_reader, writer))

            done, pending = await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)
            for task in pending: task.cancel()

        except asyncio.TimeoutError:
            logger.debug(f"客户端 {addr} 在10秒内未发送有效请求头，连接关闭。")
            if not is_counted: await self._increment_request_counter(success=False)
        except Exception as e:
            logger.error(f"处理连接 {addr} 时发生错误: {e!r}")
            if not is_counted: await self._increment_request_counter(success=False)
        finally:
            if remote_writer and not remote_writer.is_closing(): remote_writer.close()
            if not writer.is_closing(): writer.close()
            async with self.stats_lock:
                await self._save_stats()
            logger.debug(f"与 {addr} 的连接已关闭。")

    async def _cleanup_cache_dir(self, max_age_seconds: int = 600):
        """清理缓存目录中超过指定时间的旧文件"""
        try:
            now = time.time()
            cleaned_count = 0
            for filename in os.listdir(self.cache_dir):
                file_path = self.cache_dir / filename
                if file_path.is_file():
                    file_mod_time = file_path.stat().st_mtime
                    if now - file_mod_time > max_age_seconds:
                        file_path.unlink()
                        cleaned_count += 1
            if cleaned_count > 0:
                logger.info(f"清理了 {cleaned_count} 个过期的缓存图片。")
        except Exception as e:
            logger.error(f"清理缓存目录时发生错误: {e}")


    @filter.command("代理状态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status_proxy(self, event: AstrMessageEvent):

        await self._check_and_reset_daily_stats()
        is_running = self.server_task and not self.server_task.done()
        status_text = "✅运行中" if is_running else "❌已停止"
        ip_text = f"{self.current_ip}:{self.current_port}" if self.current_ip else "无"
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        async with self.stats_lock:
            succeeded = self.stats.get("today_requests_succeeded", 0)
            failed = self.stats.get("today_requests_failed", 0)
            total_ips = self.stats.get("total_ips_used", 0)
            today_ips = self.stats.get("today_ips_used", 0)
            total_traffic = self.stats.get("total_traffic_bytes", 0)
            today_traffic = self.stats.get("today_traffic_bytes", 0)
            total_traffic_limit = self.stats.get("total_traffic_limit_bytes", 0)
            daily_traffic_history = self.stats.get("daily_traffic_history", [])
        total_reqs = succeeded + failed
        success_rate_text = f"{(succeeded / total_reqs * 100):.2f}%" if total_reqs > 0 else "N/A"
        remaining_traffic_text = "无限制"
        if total_traffic_limit > 0:
            remaining_bytes = total_traffic_limit - total_traffic
            remaining_traffic_text = self._format_bytes(max(0, remaining_bytes))
        estimated_days_text = "N/A"
        avg_daily_traffic = 0
        if daily_traffic_history:
            avg_daily_traffic = sum(daily_traffic_history) / len(daily_traffic_history)
        if total_traffic_limit > 0 and avg_daily_traffic > 0 and "remaining_bytes" in locals():
            estimated_days_text = f"{(remaining_bytes / avg_daily_traffic):.2f} 天"

        if not self.config.get("enable_visual_status", False):
            status_message = (
                f"--- IP代理插件状态 ---\n"
                f"运行状态: {status_text}\n"
                f"监听地址: {listen_host}:{local_port}\n"
                f"当前代理IP: {ip_text}\n"
                f"--------------------\n"
                f"总流量限制: {self._format_bytes(total_traffic_limit) if total_traffic_limit > 0 else '无限制'}\n"
                f"总使用流量: {self._format_bytes(total_traffic)}\n"
                f"剩余流量: {remaining_traffic_text}\n"
                f"今日使用流量: {self._format_bytes(today_traffic)}\n"
                f"每日平均流量 (最近{len(daily_traffic_history)}天): {self._format_bytes(avg_daily_traffic) if daily_traffic_history else 'N/A'}\n"
                f"预计可用天数: {estimated_days_text}\n"
                f"今日请求成功率: {success_rate_text} ({succeeded}/{total_reqs})\n"
                f"IP总使用量: {total_ips}\n"
                f"今日IP使用量: {today_ips}\n"
                f"--------------------\n"
                f"白名单域名: {', '.join(self.config.get('allowed_domains', ['未配置']))}"
            )
            yield event.plain_result(status_message)
            return

        # --- 可视化卡片输出 ---
        if not VISUAL_ENABLED:
            yield event.plain_result("❌ 可视化功能未开启。缺少 jinja2 或 playwright 库，请检查安装。")
            return


        #  在生成图片前执行清理
        await self._cleanup_cache_dir(max_age_seconds=600) # 10分钟

        render_data = {
            "is_running": is_running,
            "listen_address": f"{listen_host}:{local_port}",
            "current_ip": ip_text,
            "used_traffic": self._format_bytes(total_traffic),
            "total_limit": self._format_bytes(total_traffic_limit) if total_traffic_limit > 0 else "无限制",
            "has_limit": total_traffic_limit > 0,
            "traffic_percentage": min(100, (total_traffic / total_traffic_limit) * 100) if total_traffic_limit > 0 else 0,
            "remaining_traffic": remaining_traffic_text,
            "estimated_days": estimated_days_text,
            "today_traffic": self._format_bytes(today_traffic),
            "daily_avg_traffic": self._format_bytes(avg_daily_traffic) if daily_traffic_history else "N/A",
            "success_rate": success_rate_text,
            "today_ips": today_ips,
            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        try:
            # 接收 Path 对象
            image_path = await render_status_card(render_data)
            # 传递字符串路径
            yield event.image_result(str(image_path))
        except Exception as e:
            logger.error(f"生成代理状态卡片失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 生成状态卡片时发生错误: {e}\n请确保 Playwright 已正确安装 (playwright install)。")

    async def get_new_ip(self) -> tuple[str | None, int | None]:
        api_url = self.config.get("api_url")
        if not api_url or "YOUR_TOKEN" in api_url:
            logger.warning("IP代理插件: API URL 未配置，无法获取新IP。")
            return None, None
        try:
            if self.http_session.closed: self.http_session = aiohttp.ClientSession()
            async with self.http_session.get(api_url) as response:
                response.raise_for_status()
                ip_port = (await response.text()).strip()
                if ":" in ip_port:
                    ip, port_str = ip_port.split(":")
                    port = int(port_str)
                    await self._increment_ip_usage_counter()
                    async with self.stats_lock:
                        logger.info(f"IP代理插件: 获取到新IP: {ip}:{port}。今日已使用: {self.stats['today_ips_used']}个, 总计: {self.stats['total_ips_used']}个")
                    return ip, port
                else:
                    logger.warning(f"IP代理插件: API返回格式错误: {ip_port}")
                    return None, None
        except Exception as e:
            logger.error(f"IP代理插件: 获取IP失败: {e}")
            return None, None

    @filter.command("开启代理", alias={"启动代理", "代理开启"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def start_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        if self.server_task and not self.server_task.done():
            return event.plain_result("代理服务已经在运行中。")
        self.server_task = asyncio.create_task(self.start_local_proxy_server())
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        return event.plain_result(f"代理服务已启动，监听于 {listen_host}:{local_port}")

    @filter.command("关闭代理", alias={"代理关闭", "取消代理"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def stop_proxy(self, event: AstrMessageEvent) -> MessageEventResult:
        if not self.server_task or self.server_task.done():
            return event.plain_result("代理服务未在运行。")
        self.server_task.cancel()
        try: await self.server_task
        except asyncio.CancelledError: pass
        self.server_task = None; self.server = None
        return event.plain_result("代理服务已停止。")

    @filter.command("切换IP")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_ip(self, event: AstrMessageEvent):
        yield event.plain_result("正在强制切换代理IP...")

        async with self.ip_lock:
            self.current_ip = None
            self.current_port = None
            logger.info("管理员指令: 强制切换IP，当前IP已失效。")

        new_ip, new_port = await self.get_valid_ip()

        if new_ip and new_port:
            yield event.plain_result(f"✅ IP切换成功！\n新代理IP: {new_ip}:{new_port}")
        else:
            yield event.plain_result("❌ IP切换失败！无法获取到有效的代理IP，请检查API或网络。")

    def _extract_hostname(self, request_data: bytes) -> str | None:
        try:
            request_str = request_data.decode("utf-8", errors="ignore")
            connect_match = re.search(r"CONNECT\s+([a-zA-Z0-9.-]+):\d+", request_str, re.IGNORECASE)
            if connect_match: return connect_match.group(1).lower()
            host_match = re.search(r"Host:\s+([a-zA-Z0-9.-]+)", request_str, re.IGNORECASE)
            if host_match: return host_match.group(1).lower()
        except Exception: pass
        return None

    async def is_ip_valid(self, ip: str, port: int) -> bool:
        validation_url = self.config.get("validation_url", "http://www.baidu.com")
        timeout_config = aiohttp.ClientTimeout(total=self.config.get("validation_timeout", 5))
        proxy_url = f"http://{ip}:{port}"
        try:
            if self.http_session.closed: self.http_session = aiohttp.ClientSession()
            async with self.http_session.get(validation_url, proxy=proxy_url, timeout=timeout_config) as response:
                if response.status == 200:
                    logger.info(f"IP {ip}:{port} 验证成功。")
                    return True
        except Exception as e:
            logger.warning(f"IP {ip}:{port} 访问 {validation_url} 验证失败: {e}")
        return False

    async def get_valid_ip(self) -> tuple[str | None, int | None]:
        async with self.ip_lock:
            ip_expiration_time = self.config.get("ip_expiration_time", 300)
            validation_interval = self.config.get("validation_interval", 60)
            if self.current_ip and self.current_port and self.last_validation_time:
                ip_age = time.time() - self.last_validation_time
                if ip_expiration_time > 0 and ip_age > ip_expiration_time:
                    logger.info(f"IP {self.current_ip}:{self.current_port} 已使用超过 {ip_expiration_time} 秒，强制获取新IP。")
                    self.current_ip = None
                    self.current_port = None
                elif ip_age < validation_interval:
                    logger.debug(f"使用缓存中的IP: {self.current_ip}:{self.current_port} (验证间隔内)")
                    return self.current_ip, self.current_port
                else:
                    logger.debug(f"IP {self.current_ip}:{self.current_port} 需重新验证...")
                    if await self.is_ip_valid(self.current_ip, self.current_port):
                        self.last_validation_time = time.time()
                        logger.debug(f"IP {self.current_ip}:{self.current_port} 验证成功，继续使用。")
                        return self.current_ip, self.current_port
                    else:
                        logger.info(f"IP {self.current_ip}:{self.current_port} 重新验证失败，获取新IP。")
                        self.current_ip = None

            if not self.current_ip:
                for _ in range(3):
                    new_ip, new_port = await self.get_new_ip()
                    if new_ip and new_port:
                        if await self.is_ip_valid(new_ip, new_port):
                            self.current_ip, self.current_port = new_ip, new_port
                            self.last_validation_time = time.time()
                            return self.current_ip, self.current_port
                    logger.warning("获取的新IP无效或验证失败，1秒后重试...")
                    await asyncio.sleep(1)

            if not self.current_ip:
                logger.error("多次尝试后，仍无法获取到有效的IP地址。")
                return None, None

            return self.current_ip, self.current_port

    async def start_local_proxy_server(self):
        listen_host = self.config.get("listen_host", "127.0.0.1")
        local_port = self.config.get("local_port", 8888)
        try:
            self.server = await asyncio.start_server(self.handle_connection, listen_host, local_port)
            logger.info(f"本地代理服务器已启动，监听地址: {listen_host}:{local_port}")
            await self.server.serve_forever()
        except asyncio.CancelledError:
            logger.info("本地代理服务器任务被取消。")
        except Exception as e:
            logger.error(f"启动本地代理服务器失败: {e}，请检查端口是否被占用或配置是否正确。")
        finally:
            if self.server and self.server.is_serving():
                self.server.close(); await self.server.wait_closed()
            logger.info("本地代理服务器已关闭。")
            self.server = None; self.server_task = None

    async def terminate(self):
        logger.info("IP代理插件正在终止...")
        if self.server_task and not self.server_task.done():
            self.server_task.cancel()
            try: await self.server_task
            except asyncio.CancelledError: pass
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        logger.info("IP代理插件已终止。")

    @filter.command("修改代理API")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_api_url(self, event: AstrMessageEvent, api_url: str) -> MessageEventResult:
        self.config["api_url"] = api_url
        self.config.save_config()
        return event.plain_result(f"✅ 代理API地址已更新为: {api_url}")

    @filter.command("修改监听地址")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_host(self, event: AstrMessageEvent, host: str) -> MessageEventResult:
        self.config["listen_host"] = host
        self.config.save_config()
        return event.plain_result(f"✅ 监听地址已更新为: {host}\n重启代理后生效。")

    @filter.command("修改监听端口")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_listen_port(self, event: AstrMessageEvent, port: int) -> MessageEventResult:
        self.config["local_port"] = port
        self.config.save_config()
        return event.plain_result(f"✅ 监听端口已更新为: {port}\n重启代理后生效。")

    @filter.command("修改测试url")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_validation_url(self, event: AstrMessageEvent, url: str) -> MessageEventResult:
        self.config["validation_url"] = url
        self.config.save_config()
        return event.plain_result(f"✅ 验证URL已更新为: {url}")

    @filter.command("修改IP失效时间")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_ip_expiration_time(self, event: AstrMessageEvent, seconds: int) -> MessageEventResult:
        self.config["ip_expiration_time"] = seconds
        self.config.save_config()
        if seconds > 0:
            return event.plain_result(f"✅ IP绝对失效时间已更新为: {seconds} 秒。")
        else:
            return event.plain_result("✅ IP绝对失效时间已设置为永不强制失效。")

    # --- [新增] 设置总流量限制命令 ---
    @filter.command("设置总流量")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_total_traffic_limit(self, event: AstrMessageEvent, traffic_size: str) -> MessageEventResult:
        parsed_bytes = self._parse_traffic_string(traffic_size)
        if parsed_bytes is None:
            return event.plain_result("❌ 无效的流量大小格式。请使用例如: 5GB, 1000MB, 2TB。")

        async with self.stats_lock:
            self.stats["total_traffic_limit_bytes"] = parsed_bytes
            await self._save_stats()

        return event.plain_result(f"✅ 总流量限制已设定为: {self._format_bytes(parsed_bytes)}")

    @filter.command("设置已使用流量")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_used_traffic(self, event: AstrMessageEvent, traffic_size: str) -> MessageEventResult:
        parsed_bytes = self._parse_traffic_string(traffic_size)
        if parsed_bytes is None:
            return event.plain_result("❌ 无效的流量大小格式。请使用例如: 5GB, 1000MB, 2TB。")

        async with self.stats_lock:
            self.stats["total_traffic_bytes"] = parsed_bytes
            await self._save_stats()

        return event.plain_result(f"✅ 已使用总流量已更新为: {self._format_bytes(parsed_bytes)}")
