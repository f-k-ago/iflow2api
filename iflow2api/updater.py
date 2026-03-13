"""检查更新模块"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .transport import create_upstream_transport

# GitHub 仓库信息
GITHUB_REPO = "cacaview/iflow2api"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"


@dataclass
class ReleaseInfo:
    """发布版本信息"""

    version: str  # 版本号，如 "0.3.0"
    tag_name: str  # Git 标签名，如 "v0.3.0"
    html_url: str  # Release 页面 URL
    published_at: datetime  # 发布时间
    body: str  # Release notes (markdown)
    prerelease: bool  # 是否为预发布版本


def get_current_version() -> str:
    """获取当前版本号

    Returns:
        当前版本号字符串，如 "0.2.0"
    """
    # 尝试从 pyproject.toml 获取版本
    try:
        from pathlib import Path

        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        if pyproject_path.exists():
            content = pyproject_path.read_text(encoding="utf-8")
            # 匹配 version = "x.y.z"
            match = re.search(r'version\s*=\s*"([^"]+)"', content)
            if match:
                return match.group(1)
    except Exception:
        pass

    # 尝试从 __init__.py 获取版本
    try:
        from . import __version__

        return __version__
    except ImportError:
        pass

    # 默认版本
    return "0.0.0"


def parse_version(version_str: str) -> tuple[int, ...]:
    """解析版本号字符串为元组

    Args:
        version_str: 版本号字符串，如 "0.2.0" 或 "v0.2.0"

    Returns:
        版本号元组，如 (0, 2, 0)
    """
    # 移除 'v' 前缀
    version_str = version_str.lstrip("v")

    # 提取数字部分
    parts = re.findall(r"\d+", version_str)

    # 转换为整数元组，不足3位补0
    result = tuple(int(p) for p in parts[:3])
    while len(result) < 3:
        result = result + (0,)

    return result


def compare_versions(v1: str, v2: str) -> int:
    """比较两个版本号

    Args:
        v1: 第一个版本号
        v2: 第二个版本号

    Returns:
        -1 如果 v1 < v2
        0 如果 v1 == v2
        1 如果 v1 > v2
    """
    p1 = parse_version(v1)
    p2 = parse_version(v2)

    if p1 < p2:
        return -1
    elif p1 > p2:
        return 1
    return 0


async def get_latest_release(timeout: float = 10.0) -> Optional[ReleaseInfo]:
    """获取最新发布版本信息

    Args:
        timeout: 请求超时时间（秒）

    Returns:
        ReleaseInfo 对象，如果请求失败则返回 None
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"iflow2api/{get_current_version()}",
    }

    client = None
    try:
        # 加载代理与传输层配置
        from .settings import get_effective_upstream_transport_backend, load_settings

        settings = load_settings()
        proxy = settings.upstream_proxy if settings.upstream_proxy_enabled and settings.upstream_proxy else None

        client = create_upstream_transport(
            backend=get_effective_upstream_transport_backend(settings),
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy,
            trust_env=False,
            impersonate=settings.tls_impersonate,
        )

        response = await client.get(GITHUB_API_URL, headers=headers, timeout=timeout)

        if response.status_code != 200:
            return None

        data = response.json()

        # 解析发布时间
        published_at = datetime.fromisoformat(
            data["published_at"].replace("Z", "+00:00")
        )

        return ReleaseInfo(
            version=data["tag_name"].lstrip("v"),
            tag_name=data["tag_name"],
            html_url=data["html_url"],
            published_at=published_at,
            body=data.get("body", ""),
            prerelease=data.get("prerelease", False),
        )
    except Exception:
        return None
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def check_for_updates(
    current_version: Optional[str] = None,
) -> tuple[bool, Optional[ReleaseInfo]]:
    """检查是否有更新

    Args:
        current_version: 当前版本号，如果不提供则自动获取

    Returns:
        (是否有更新, 最新版本信息)
    """
    if current_version is None:
        current_version = get_current_version()

    release = await get_latest_release()
    if release is None:
        return False, None

    has_update = compare_versions(current_version, release.version) < 0
    return has_update, release


def format_release_notes(body: str, max_length: int = 500) -> str:
    """格式化 Release Notes

    Args:
        body: 原始 markdown 内容
        max_length: 最大长度

    Returns:
        格式化后的文本
    """
    if not body:
        return ""

    # 移除 markdown 标题标记
    text = re.sub(r"^#+\s*", "", body, flags=re.MULTILINE)

    # 移除多余的空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 截断过长的内容
    if len(text) > max_length:
        text = text[:max_length].rsplit("\n", 1)[0] + "\n..."

    return text.strip()
