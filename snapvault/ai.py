"""AI 视觉描述命名：用通义千问 Qwen-VL 为截图生成一句话中文描述.

设计目标
--------
1. **零重型依赖**：只用标准库 :mod:`urllib.request` 调通义千问 DashScope 兼容的
   OpenAI 风格接口（``https://dashscope.aliyuncs.com/compatible-mode/v1``），
   不引入 ``requests`` / ``openai`` 等第三方包。
2. **绝不断档**：任何失败（无 Key / 网络超时 / 鉴权失败 / 返回异常）都抛
   :class:`AiError` 或返回空，由调用方（vault）回退到时间戳命名，绝不阻塞截图主流程。
3. **可插拔 / 可测**：``describe()`` 是纯函数，测试里用 ``transport`` 注入假响应。

描述被压缩成可用于文件名的 slug（中文短句，供 ``naming.slugify`` 进一步清洗）。
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from PIL import Image

#: 默认模型（qwen3-vl-plus 效果最好；qwen3-vl-flash 更快更省）
DEFAULT_MODEL = "qwen3-vl-plus"
#: DashScope 兼容 OpenAI 风格的 endpoint（旧公共域名，官方标注"仍可正常使用"）
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1"

#: 分类键说明（与 config.DEFAULT_CATEGORIES 对齐；这里冗余一份，避免 config <-> ai 循环导入）
CATEGORY_HINTS = (
    "code=代码或终端, document=文档或阅读页, webpage=网页或应用界面, chat=聊天会话, "
    "table=表格或数据表, image=照片或图片, design=设计稿或图表, video=视频画面, "
    "game=游戏画面, other=以上都不是"
)

#: 默认提示词：**一次请求同时要「描述 + 分类」**，输出 JSON 便于稳定解析。
#: desc 用于文件名，cat 用于覆盖本地视觉规则分类（AI 看懂了内容，判定通常更准）。
#: 字数上限写在提示词里（而不是只靠 max_desc_len 硬截）——模型自己收着写，
#: 就不会出现"…代码编辑器的半截字"这种生硬截断。
DEFAULT_PROMPT = (
    "看这张截图，只输出一个 JSON 对象（不要代码块标记、不要任何解释）：\n"
    '{"desc": "<一句话中文描述，严格不超过 24 个字>", "cat": "<分类键>"}\n'
    f"cat 必须取以下之一：{CATEGORY_HINTS}\n"
    "desc 概括画面主体（例如某个软件界面、某段代码、某张表格），"
    "宁可少写也别超字数。"
)

_WHITESPACE = re.compile(r"\s+")
_STRIP_QUOTES = re.compile(r'^[\s"“”\'‘’`]+|[\s"“”\'‘’`]+$')

#: 用户从控制台复制时可能连带带上的请求路径尾巴
_REQUEST_TAILS = ("/chat/completions", "/completions", "/responses", "/embeddings")


def normalize_endpoint(raw: str) -> str:
    """把用户粘贴的 Base URL 归一成可拼接 ``/chat/completions`` 的形式。

    百炼现在给每个业务空间发了专属域名（形如
    ``https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com``），和旧的公共域名
    ``dashscope.aliyuncs.com`` 长得不一样；而控制台上"Base URL"那一行很容易
    被连着后面的路径一起复制。这里统一兜住：

    ===========================================================  ==========================================
    输入                                                          输出
    ===========================================================  ==========================================
    ``（空）``                                                    ``DEFAULT_ENDPOINT``
    ``https://ws-x.cn-beijing.maas.aliyuncs.com/compatible-mode/v1``  原样
    ``…/compatible-mode/v1/``                                     去尾斜杠
    ``…/compatible-mode/v1/chat/completions``                     去掉请求路径
    ``ws-x.cn-beijing.maas.aliyuncs.com``                         只给域名 → 补 ``/compatible-mode/v1``
    ``https://my-proxy.local/v1``                                 自建代理 → 保留自有路径
    ===========================================================  ==========================================
    """
    s = (raw or "").strip().strip('"').strip("'").strip()
    if not s:
        return DEFAULT_ENDPOINT
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    s = s.rstrip("/")
    for tail in _REQUEST_TAILS:
        if s.endswith(tail):
            s = s[: -len(tail)].rstrip("/")
            break
    parts = urlsplit(s)
    if not parts.netloc:
        return DEFAULT_ENDPOINT
    if not parts.path or parts.path == "/":
        # 只给了域名，补上百炼兼容模式的标准路径
        return urlunsplit((parts.scheme or "https", parts.netloc, "/compatible-mode/v1", "", ""))
    return s


class AiError(RuntimeError):
    """AI 描述失败（无 Key / 网络 / 鉴权 / 返回异常）。"""


@dataclass
class AiResult:
    """一次 AI 描述的结果。

    ``category`` 是模型给出的分类键（可能为空——模型没按 JSON 返回时，
    或者它给了一个不认识的键）。**是否采信由上层决定**：vault 层会先校验它
    是否在 ``cfg.categories`` 里，合法才用来覆盖本地视觉规则分类。
    """

    description: str
    category: str = ""
    model: str = ""
    elapsed_ms: int = 0
    raw: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.description.strip())


def _clean(text: str) -> str:
    """把模型输出清洗成一句可用于文件名的短描述。"""
    if not text:
        return ""
    text = _STRIP_QUOTES.sub("", text)
    text = text.replace("\n", " ").replace("\r", " ")
    text = _WHITESPACE.sub(" ", text)
    # 去掉模型偶尔带上的序号 / 标签 / 冒号前缀
    text = re.sub(r"^(描述|内容|这是一张|截图内容?)[:：]\s*", "", text)
    text = text.strip(" ，。,.!！?？;；")
    return text


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _parse_payload(text: str) -> tuple[str, str]:
    """从模型输出里取出 ``(描述, 分类键)``。

    兼容三种情形：

    1. 规范 JSON：``{"desc": "股票行情图", "cat": "table"}``
    2. 被 ```json 代码块裹住的 JSON（有些模型爱加这层）
    3. 纯文本一句话（模型没按格式来）→ 整段当描述、分类留空，
       上层回落本地视觉规则分类，不会因为格式跑偏就把内容丢掉

    以 ``{`` 开头却解析失败时**返回空**——宁可让上层降级成时间戳命名，
    也不要把 JSON 碎片当文件名。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""

    candidate = raw
    block = _JSON_BLOCK.search(raw)
    if block:
        candidate = block.group(1).strip()

    if candidate.startswith("{"):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            return "", ""
        if isinstance(data, dict):
            desc = str(data.get("desc") or data.get("description") or "")
            cat = str(data.get("cat") or data.get("category") or "")
            return _clean(desc), _clean(cat).lower()
        return "", ""

    return _clean(raw), ""


def _encode_image(image: Image.Image, max_edge: int = 1568) -> str:
    """把 PIL 图像缩小后转成 data URL（base64），控制体积与令牌消耗。"""
    img = image if image.mode == "RGB" else image.convert("RGB")
    w, h = img.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


class QwenVision:
    """通义千问 Qwen-VL 视觉描述客户端。"""

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        prompt: str = DEFAULT_PROMPT,
        timeout: float = 15.0,
        max_desc_len: int = 24,
        requires_key: bool = True,
        transport: Optional[Callable[[urllib.request.Request, float], Any]] = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model or DEFAULT_MODEL
        self.endpoint = normalize_endpoint(endpoint)
        self.prompt = prompt or DEFAULT_PROMPT
        self.timeout = timeout
        self.max_desc_len = max_desc_len
        #: 是否必须要有 Key。Ollama 这类本地端点是 False —— 它压根不校验
        #: Authorization，要用户为本地服务去申请一个 Key 没有意义。
        self.requires_key = requires_key
        #: 测试注入：``transport(req, timeout) -> HTTPResponse-like``
        self._transport = transport

    @property
    def available(self) -> bool:
        return bool(self.api_key) or not self.requires_key

    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        """请求头。没有 Key 时**不发** Authorization —— 某些兼容端点在收到
        ``Bearer `` （空令牌）时会直接 401，而本地 Ollama 根本不需要它。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def describe(self, image: Image.Image, when=None) -> AiResult:
        """为一张截图生成描述。失败抛 :class:`AiError`。"""
        if not self.available:
            raise AiError("未配置 API Key" if self.requires_key else "客户端不可用")
        if image is None or image.width <= 0 or image.height <= 0:
            raise AiError("空图像")

        started = time.time()
        data_url = _encode_image(image)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": self.prompt},
                    ],
                }
            ],
            "max_tokens": 160,          # 要塞下描述 + 分类键，比纯描述版本留宽些
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            if self._transport is not None:
                resp = self._transport(req, self.timeout)
                body = resp.read() if hasattr(resp, "read") else resp
                status = getattr(resp, "status", 200)
                if isinstance(body, bytes):
                    body = body.decode("utf-8", "replace")
            else:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise AiError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AiError(f"网络错误: {exc}") from exc

        if status != 200:
            raise AiError(f"HTTP {status}: {body[:300]}")

        desc, category = _parse_payload(self._extract(body))
        if not desc:
            raise AiError("模型未返回有效描述")
        if self.max_desc_len and len(desc) > self.max_desc_len:
            desc = desc[: self.max_desc_len].rstrip(" ，。,.!！?？;；")
        return AiResult(
            description=desc,
            category=category,
            model=self.model,
            elapsed_ms=int((time.time() - started) * 1000),
            raw=body[:500],
        )

    # ------------------------------------------------------------------ #

    def _extract(self, body: str) -> str:
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AiError(f"响应不是合法 JSON: {body[:200]}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise AiError(f"API 错误: {json.dumps(data['error'], ensure_ascii=False)[:200]}")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AiError(f"响应结构异常: {body[:200]}") from exc

    # ------------------------------------------------------------------ #
    # 探测能力：给设置界面「测试连接」和「拉取模型列表」用
    # ------------------------------------------------------------------ #

    def list_models(self) -> list[str]:
        """向 ``GET {endpoint}/models`` 问服务端有哪些模型。

        Ollama 和百炼的兼容模式都实现了这个端点。拉不到就返回空列表 ——
        调用方照常用预设候选，不因为探测失败就挡住用户。
        """
        req = urllib.request.Request(f"{self.endpoint}/models",
                                     headers=self._headers(), method="GET")
        try:
            if self._transport is not None:
                resp = self._transport(req, self.timeout)
                body = resp.read() if hasattr(resp, "read") else resp
                if isinstance(body, bytes):
                    body = body.decode("utf-8", "replace")
            else:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
        except Exception:
            return []
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
        items = data.get("data") if isinstance(data, dict) else data
        out: list[str] = []
        if isinstance(items, list):
            for item in items:
                mid = item.get("id") if isinstance(item, dict) else item
                if isinstance(mid, str) and mid.strip():
                    out.append(mid.strip())
        return out

    def test_connection(self) -> tuple[bool, str]:
        """探活。返回 ``(是否可用, 给人看的一句话)``。

        先试 ``/models``（最轻，且顺带能验证 Key 和端点都对）；不通再给一条
        可操作的提示，别只报 "connection refused" 让用户自己猜。
        """
        if not self.available:
            return False, ("未填写 API Key" if self.requires_key else "客户端不可用")
        models = self.list_models()
        if models:
            return True, f"连接正常，服务端有 {len(models)} 个模型"
        # /models 不通不代表整体不可用（有些兼容层不实现它），再用一次真实
        # 请求确认 —— 但传一张 1×1 的图，省钱也省时间。
        try:
            probe = Image.new("RGB", (8, 8), (255, 255, 255))
            self.describe(probe)
            return True, "连接正常（服务端未提供模型列表）"
        except AiError as exc:
            msg = str(exc)
            if "11434" in self.endpoint and ("拒绝" in msg or "refused" in msg.lower()):
                return False, "连不上本机 Ollama：请确认已安装并运行（ollama serve）"
            return False, msg[:120]
        except Exception as exc:                                # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:120]


# --------------------------------------------------------------------------- #
# 便捷工厂 / 缓存
# --------------------------------------------------------------------------- #

_CACHE: dict[str, tuple[float, AiResult]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 3600.0  # 同图 1 小时内复用描述（内容哈希为键）


def _cache_key(image_bytes: bytes) -> str:
    import hashlib

    return hashlib.sha1(image_bytes).hexdigest()


def describe_cached(client: QwenVision, image: Image.Image, image_bytes: bytes) -> AiResult:
    """带内容缓存的描述：相同图片 1 小时内不重复请求（省额度、加速）。"""
    key = _cache_key(image_bytes or b"")
    if key:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit and time.time() - hit[0] < _CACHE_TTL:
                return hit[1]
    result = client.describe(image)
    if key and result.ok:
        with _CACHE_LOCK:
            _CACHE[key] = (time.time(), result)
    return result
