"""Vision 支持 - 图像输入处理

支持图像输入的多模态功能，包括：
- Base64 编码图像
- 图像 URL
- OpenAI 和 Anthropic 格式兼容

支持的视觉模型：
- Qwen-VL-Max (通义千问)
"""

import base64
import hashlib
from typing import Optional, Any
from dataclasses import dataclass

from .transport import create_upstream_transport


# 支持视觉功能的模型列表
VISION_MODELS = {
    # Qwen 视觉模型
    "qwen-vl-max": {"name": "Qwen-VL-Max", "provider": "alibaba", "max_images": 10},
}

# 默认视觉模型（当请求包含图像但模型不支持时回退）
DEFAULT_VISION_MODEL = "qwen-vl-max"


@dataclass
class ImageData:
    """图像数据"""
    data: str  # Base64 编码数据或 URL
    is_url: bool = False
    media_type: str = "image/png"  # MIME 类型
    detail: str = "auto"  # OpenAI detail 参数: auto, low, high


def is_vision_model(model: str) -> bool:
    """检查模型是否支持视觉功能"""
    return model.lower() in VISION_MODELS


def get_vision_model_info(model: str) -> Optional[dict]:
    """获取视觉模型信息"""
    return VISION_MODELS.get(model.lower())


def supports_vision(model: str) -> bool:
    """检查模型是否支持视觉功能（别名）"""
    return is_vision_model(model)


def get_max_images(model: str) -> int:
    """获取模型支持的最大图像数量"""
    info = get_vision_model_info(model)
    return info["max_images"] if info else 0


def detect_image_content(content: Any) -> list[ImageData]:
    """
    从消息内容中检测并提取图像
    
    支持格式：
    1. OpenAI 格式:
       [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "..."}}]
    
    2. Anthropic 格式:
       [{"type": "text", "text": "..."}, {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}]
    
    Args:
        content: 消息内容（字符串或内容块列表）
    
    Returns:
        提取的 ImageData 列表
    """
    images = []
    
    if not isinstance(content, list):
        return images
    
    for block in content:
        if not isinstance(block, dict):
            continue
        
        block_type = block.get("type", "")
        
        # OpenAI 格式: image_url
        if block_type == "image_url":
            image_url = block.get("image_url", {})
            url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
            
            if url:
                if url.startswith("data:"):
                    # Base64 data URL: data:image/png;base64,xxxxx
                    try:
                        media_type, data = parse_data_url(url)
                        images.append(ImageData(
                            data=data,
                            is_url=False,
                            media_type=media_type,
                            detail=image_url.get("detail", "auto") if isinstance(image_url, dict) else "auto"
                        ))
                    except ValueError:
                        # 无法解析的 data URL，当作普通 URL 处理
                        images.append(ImageData(data=url, is_url=True))
                else:
                    # 普通 URL
                    images.append(ImageData(
                        data=url,
                        is_url=True,
                        detail=image_url.get("detail", "auto") if isinstance(image_url, dict) else "auto"
                    ))
        
        # Anthropic 格式: image
        elif block_type == "image":
            source = block.get("source", {})
            if source.get("type") == "base64":
                images.append(ImageData(
                    data=source.get("data", ""),
                    is_url=False,
                    media_type=source.get("media_type", "image/png")
                ))
            elif source.get("type") == "url":
                images.append(ImageData(
                    data=source.get("url", ""),
                    is_url=True
                ))
    
    return images


def parse_data_url(data_url: str) -> tuple[str, str]:
    """
    解析 data URL
    
    格式: data:[<mediatype>][;base64],<data>
    
    Args:
        data_url: data URL 字符串
    
    Returns:
        (media_type, data) 元组
    
    Raises:
        ValueError: 如果 data URL 格式无效
    """
    if not data_url.startswith("data:"):
        raise ValueError("Not a data URL")
    
    # 移除 "data:" 前缀
    rest = data_url[5:]
    
    # 查找逗号分隔符
    comma_idx = rest.find(",")
    if comma_idx == -1:
        raise ValueError("Invalid data URL: missing comma")
    
    # 解析媒体类型
    media_type_part = rest[:comma_idx]
    data_part = rest[comma_idx + 1:]
    
    if ";base64" in media_type_part:
        media_type = media_type_part.replace(";base64", "")
    else:
        media_type = media_type_part
    
    # 默认媒体类型
    if not media_type:
        media_type = "image/png"
    
    return media_type, data_part


def image_to_base64(data: bytes, media_type: str = "image/png") -> str:
    """
    将图像字节转换为 Base64 编码字符串
    
    Args:
        data: 图像字节数据
        media_type: MIME 类型
    
    Returns:
        Base64 编码的 data URL
    """
    encoded = base64.b64encode(data).decode("utf-8")
    return f"data:{media_type};base64,{encoded}"


async def fetch_image_as_base64(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """
    从 URL 获取图像并转换为 Base64

    Args:
        url: 图像 URL
        timeout: 超时时间（秒）

    Returns:
        (base64_data, media_type) 元组
    """
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

        response = await client.get(url, timeout=timeout)
        response.raise_for_status()

        # 从 Content-Type 获取媒体类型
        content_type = response.headers.get("content-type", "image/png")
        # 移除可能的额外参数（如 charset）
        media_type = content_type.split(";")[0].strip()

        # 验证是否为图像类型
        if not media_type.startswith("image/"):
            media_type = "image/png"  # 默认

        data = base64.b64encode(response.content).decode("utf-8")
        return data, media_type
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def convert_to_openai_format(images: list[ImageData]) -> list[dict]:
    """
    将图像数据转换为 OpenAI 格式
    
    OpenAI 格式:
    [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxxxx"}}
    ]
    
    Args:
        images: ImageData 列表
    
    Returns:
        OpenAI 格式的内容块列表
    """
    blocks = []
    
    for img in images:
        if img.is_url:
            # URL 格式
            blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": img.data,
                    "detail": img.detail
                }
            })
        else:
            # Base64 格式，构建 data URL
            data_url = f"data:{img.media_type};base64,{img.data}"
            blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": data_url,
                    "detail": img.detail
                }
            })
    
    return blocks


def convert_to_anthropic_format(images: list[ImageData]) -> list[dict]:
    """
    将图像数据转换为 Anthropic 格式
    
    Anthropic 格式:
    [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "xxxxx"
            }
        }
    ]
    
    Args:
        images: ImageData 列表
    
    Returns:
        Anthropic 格式的内容块列表
    """
    blocks = []
    
    for img in images:
        if img.is_url:
            # Anthropic 也支持 URL 类型的 source
            blocks.append({
                "type": "image",
                "source": {
                    "type": "url",
                    "url": img.data
                }
            })
        else:
            # Base64 格式
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": img.data
                }
            })
    
    return blocks


def process_message_content(content: Any, target_format: str = "openai") -> Any:
    """
    处理消息内容，确保图像格式正确
    
    Args:
        content: 消息内容
        target_format: 目标格式 ("openai" 或 "anthropic")
    
    Returns:
        处理后的内容
    """
    if not isinstance(content, list):
        return content
    
    # 提取文本和图像
    text_parts = []
    images = []
    
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict):
            block_type = block.get("type", "")
            
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "image_url":
                # OpenAI 格式图像
                image_url = block.get("image_url", {})
                url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
                if url:
                    if url.startswith("data:"):
                        media_type, data = parse_data_url(url)
                        images.append(ImageData(
                            data=data,
                            is_url=False,
                            media_type=media_type,
                            detail=image_url.get("detail", "auto") if isinstance(image_url, dict) else "auto"
                        ))
                    else:
                        images.append(ImageData(
                            data=url,
                            is_url=True,
                            detail=image_url.get("detail", "auto") if isinstance(image_url, dict) else "auto"
                        ))
            elif block_type == "image":
                # Anthropic 格式图像
                source = block.get("source", {})
                if source.get("type") == "base64":
                    images.append(ImageData(
                        data=source.get("data", ""),
                        is_url=False,
                        media_type=source.get("media_type", "image/png")
                    ))
                elif source.get("type") == "url":
                    images.append(ImageData(
                        data=source.get("url", ""),
                        is_url=True
                    ))
    
    # 构建新的内容块
    new_blocks = []
    
    # 添加文本
    if text_parts:
        combined_text = "\n".join(text_parts)
        if combined_text.strip():
            new_blocks.append({"type": "text", "text": combined_text})
    
    # 添加图像
    if target_format == "openai":
        new_blocks.extend(convert_to_openai_format(images))
    else:
        new_blocks.extend(convert_to_anthropic_format(images))
    
    return new_blocks if new_blocks else content


def get_image_hash(data: str) -> str:
    """获取图像数据的哈希值（用于缓存键）"""
    return hashlib.md5(data.encode()).hexdigest()[:16]


def estimate_image_tokens(image: ImageData) -> int:
    """
    估算图像的 token 数量
    
    基于 OpenAI 的估算方法：
    - low detail: 固定 85 tokens
    - high detail: 基于 512px 切片计算
    - auto: 使用 high detail 估算
    
    Args:
        image: 图像数据
    
    Returns:
        估算的 token 数量
    """
    detail = image.detail
    
    if detail == "low":
        return 85
    
    # high 或 auto: 使用更复杂的估算
    # 假设图像为 1024x1024，每 512px 切片约 170 tokens
    # 这是一个粗略估算，实际值取决于图像大小
    # 对于 Base64 数据，可以尝试解码获取实际尺寸
    
    # 默认估算值（假设中等大小图像）
    return 765  # 5 个切片 * 170 + 85 base


def validate_image_data(data: str, is_base64: bool = True) -> bool:
    """
    验证图像数据是否有效
    
    Args:
        data: 图像数据（Base64 或 URL）
        is_base64: 是否为 Base64 数据
    
    Returns:
        是否有效
    """
    if not data:
        return False
    
    if is_base64:
        try:
            # 尝试解码 Base64
            decoded = base64.b64decode(data, validate=True)
            # 检查是否有合理的图像大小（至少 100 字节）
            return len(decoded) >= 100
        except Exception:
            return False
    else:
        # URL 验证
        return data.startswith(("http://", "https://", "data:"))


def get_vision_models_list() -> list[dict]:
    """获取所有支持的视觉模型列表"""
    return [
        {
            "id": model_id,
            "name": info["name"],
            "provider": info["provider"],
            "max_images": info["max_images"],
            "supports_vision": True,
        }
        for model_id, info in VISION_MODELS.items()
    ]
