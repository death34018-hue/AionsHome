"""
局域网零配置发现：通过 mDNS (Bonjour/Avahi) 广播服务地址。
手机 / 其他设备可通过 http://aion-home.local:8080 访问，无需关心 IP 变化。
"""

import asyncio
import socket
import logging

logger = logging.getLogger("mdns")

_SERVICE_NAME: str | None = None


def _get_lan_ip() -> str:
    """获取本机当前局域网 IPv4 地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_local_address() -> str:
    """返回本机局域网地址，启动时打印。"""
    return _get_lan_ip()


async def start_mdns_advertise(port: int = 8080, name: str = "aion-home") -> None:
    """
    启动 mDNS 广播，注册 aion-home.local 主机名。
    如果 zeroconf 未安装，静默跳过（不影响主流程）。
    """
    global _SERVICE_NAME
    try:
        from zeroconf import Zeroconf, ServiceInfo
        from zeroconf._utils.net import get_all_addresses
    except ImportError:
        logger.info("zeroconf not installed, skipping mDNS")
        return

    try:
        ip = _get_lan_ip()
        # 确保 IP 在可用网卡中
        all_ips = set(get_all_addresses())
        if ip not in all_ips:
            for addr in sorted(all_ips):
                if addr != "127.0.0.1" and not addr.startswith("169.254"):
                    ip = addr
                    break

        hostname = f"{name}.local."
        _SERVICE_NAME = hostname

        info = ServiceInfo(
            "_http._tcp.local.",
            f"{name}._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={"path": "/chat", "version": "1.0"},
            server=f"{name}.local.",
        )

        # 在单独线程运行 zeroconf，避免和 asyncio 事件循环冲突
        loop = asyncio.get_event_loop()

        def _run_zeroconf():
            zc = Zeroconf(interfaces=[ip] if ip != "127.0.0.1" else None)
            zc.register_service(info)
            return zc

        zc = await loop.run_in_executor(None, _run_zeroconf)
        logger.info(f"mDNS started: http://{name}.local:{port}  ->  {ip}:{port}")
        print(f"[mDNS] http://{name}.local:{port}")

        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            await loop.run_in_executor(None, lambda: (zc.unregister_service(info), zc.close()))
            logger.info("mDNS stopped")
    except Exception as e:
        import traceback
        print(f"[mDNS] startup failed (non-critical): {e}")
        traceback.print_exc()
        logger.warning(f"mDNS 启动失败（不影响主服务）: {e}")


def print_lan_address(port: int = 8080) -> None:
    """启动时打印局域网访问地址。"""
    ip = _get_lan_ip()
    print("=" * 55)
    print(f"  LAN Access:")
    print(f"     http://{ip}:{port}/chat")
    print(f"  Open above URL in your phone browser to connect")
    print("=" * 55)
