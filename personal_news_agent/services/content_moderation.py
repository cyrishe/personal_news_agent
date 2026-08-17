from __future__ import annotations

import json
import os
import importlib.util
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


LLM_QUERY_MODERATION_SERVICE = "llm_query_moderation"
DEFAULT_DISCRIMINATIVE_MODEL_DIR = Path(__file__).resolve().parents[2] / "判别式ai" / "artifacts"


class ContentModerationError(RuntimeError):
    # 内容安全检测本身不可用时抛出，例如缺少密钥、缺少 SDK 或阿里云返回结构异常。
    def __init__(
        self,
        message: str,
        *,
        provider_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_code = provider_code
        self.request_id = request_id


@dataclass(frozen=True)
class ContentModerationResult:
    # 统一返回给业务层的检测结果，业务层只需要优先看 allowed。
    allowed: bool
    code: int | str | None
    message: str
    risk_level: str | None
    label: str | None
    description: str | None
    request_id: str | None
    raw: dict[str, Any]


class TextModerationPlusService:
    """阿里云内容安全 TextModerationPlus 的轻量封装。"""

    def __init__(
        self,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        endpoint: str | None = None,
        query_service: str | None = None,
        fail_open: bool = True,
        discriminative_model_dir: str | Path | None = None,
        discriminative_threshold: float | None = None,
    ):
        # 显式传参优先；环境只认项目现有 .env 的唯一命名，不做别名猜测。
        self.access_key_id = access_key_id or os.getenv("AccessKeyID")
        self.access_key_secret = access_key_secret or os.getenv("AccessKeySecret")
        # TextModerationPlus 调试成功时用的是 green-cip.cn-shanghai.aliyuncs.com，可通过环境变量覆盖。
        self.endpoint = endpoint or os.getenv("ALIYUN_CONTENT_MODERATION_ENDPOINT", "green-cip.cn-shanghai.aliyuncs.com")
        # 用户输入和模型输出分别使用不同审核服务类型，必要时可以用环境变量分别覆盖。
        self.query_service = query_service or os.getenv(
            "ALIYUN_CONTENT_MODERATION_QUERY_SERVICE",
            LLM_QUERY_MODERATION_SERVICE,
        )
        self.fail_open = bool(fail_open)
        self.discriminative_model_dir = Path(
            discriminative_model_dir
            or os.getenv("PNA_DISCRIMINATIVE_MODEL_DIR")
            or DEFAULT_DISCRIMINATIVE_MODEL_DIR
        )
        self.discriminative_threshold = (
            float(os.getenv("PNA_DISCRIMINATIVE_MODEL_THRESHOLD", "0.5"))
            if discriminative_threshold is None
            else discriminative_threshold
        )
        if not 0 <= self.discriminative_threshold <= 1:
            raise ValueError("PNA_DISCRIMINATIVE_MODEL_THRESHOLD must be between 0 and 1")
        self._discriminative_runtime: tuple[Any, Any, dict[str, Any], Any] | None = None
        self._discriminative_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.access_key_id and self.access_key_secret) or self._discriminative_model_available

    @property
    def _aliyun_configured(self) -> bool:
        return bool(self.access_key_id and self.access_key_secret)

    @property
    def _discriminative_model_available(self) -> bool:
        required_files = ("model.pt", "config.json", "vocab.json")
        return all((self.discriminative_model_dir / name).is_file() for name in required_files)

    def check_query_text(
        self,
        text: str,
        *,
        account_id: str | None = None,
        data_id: str | None = None,
    ) -> ContentModerationResult:
        """同时执行阿里云 query 审核和本地判别模型审核。"""
        if not self.configured:
            raise ContentModerationError("No content moderation service or local model is configured")
        if not text.strip():
            return self._empty_result()

        aliyun_result: ContentModerationResult | None = None
        local_result: ContentModerationResult | None = None
        errors: dict[str, str] = {}
        provider_errors: list[ContentModerationError] = []

        if self._aliyun_configured:
            try:
                aliyun_result = self._check_text_with_service(
                    text,
                    self.query_service,
                    account_id=account_id,
                    data_id=data_id,
                )
            except ContentModerationError as exc:
                errors["aliyun"] = str(exc)
                provider_errors.append(exc)

        if self._discriminative_model_available:
            try:
                local_result = self._check_with_discriminative_model(text)
            except ContentModerationError as exc:
                errors["discriminative_model"] = str(exc)
                provider_errors.append(exc)

        completed = [result for result in (aliyun_result, local_result) if result is not None]
        if not completed:
            details = "; ".join(f"{name}: {error}" for name, error in errors.items())
            first_error = provider_errors[0] if provider_errors else None
            raise ContentModerationError(
                details or "All content moderation checks are unavailable",
                provider_code=getattr(first_error, "provider_code", None),
                request_id=getattr(first_error, "request_id", None),
            )

        blocked = next((result for result in completed if not result.allowed), None)
        representative = blocked or completed[0]
        return ContentModerationResult(
            allowed=all(result.allowed for result in completed),
            code=representative.code,
            message=representative.message,
            risk_level=representative.risk_level,
            label=representative.label,
            description=representative.description,
            request_id=representative.request_id,
            raw={
                "aliyun": aliyun_result.raw if aliyun_result else None,
                "discriminative_model": local_result.raw if local_result else None,
                "errors": errors,
            },
        )

    def _check_text_with_service(
        self,
        text: str,
        service: str,
        *,
        account_id: str | None = None,
        data_id: str | None = None,
    ) -> ContentModerationResult:
        if not self._aliyun_configured:
            raise ContentModerationError("Aliyun content moderation access key is not configured")
        if not text.strip():
            return self._empty_result()

        try:
            payload = self._call_text_moderation_plus(
                text,
                service,
                account_id=account_id,
                data_id=data_id,
            )
        except ContentModerationError:
            raise
        except Exception as exc:
            raise ContentModerationError(
                f"Aliyun TextModerationPlus request failed: {type(exc).__name__}",
                provider_code=getattr(exc, "code", None),
                request_id=getattr(exc, "request_id", None),
            ) from exc
        return self._parse_result(payload)

    @staticmethod
    def _empty_result() -> ContentModerationResult:
        # 空输入没有必要调用远端 API 或本地模型，直接视为通过。
        return ContentModerationResult(
            allowed=True,
            code=200,
            message="empty input",
            risk_level="none",
            label="nonLabel",
            description="empty input skipped",
            request_id=None,
            raw={},
        )

    def _check_with_discriminative_model(self, text: str) -> ContentModerationResult:
        torch, tokenizer, config, model = self._load_discriminative_runtime()
        token_ids, attention_mask = tokenizer.encode(text, int(config["max_length"]))
        with torch.no_grad():
            violation_logits, tag_logits = model(
                torch.tensor([token_ids]),
                torch.tensor([attention_mask], dtype=torch.bool),
            )
            probability = float(torch.sigmoid(violation_logits).item())
            tag = int(tag_logits.argmax(dim=1).item()) + 1

        allowed = probability < self.discriminative_threshold
        return ContentModerationResult(
            allowed=allowed,
            code=200,
            message="local discriminative model completed",
            risk_level="none" if allowed else "high",
            label="nonLabel" if allowed else f"discriminative_tag_{tag}",
            description=(
                f"本地判别模型通过，风险概率 {probability:.2%}"
                if allowed
                else f"本地判别模型判定为不安全，风险概率 {probability:.2%}，tag={tag}"
            ),
            request_id=None,
            raw={
                "allowed": allowed,
                "probability": probability,
                "threshold": self.discriminative_threshold,
                "tag": tag,
                "model_dir": str(self.discriminative_model_dir),
            },
        )

    def _load_discriminative_runtime(self) -> tuple[Any, Any, dict[str, Any], Any]:
        if self._discriminative_runtime is not None:
            return self._discriminative_runtime
        with self._discriminative_lock:
            if self._discriminative_runtime is not None:
                return self._discriminative_runtime
            try:
                import torch

                project_dir = self.discriminative_model_dir.parent
                model_module = self._load_python_module(
                    "pna_discriminative_model", project_dir / "model.py"
                )
                tokenizer_module = self._load_python_module(
                    "pna_discriminative_tokenizer", project_dir / "tokenizer.py"
                )
                config = json.loads(
                    (self.discriminative_model_dir / "config.json").read_text(encoding="utf-8")
                )
                tokenizer = tokenizer_module.CharTokenizer.load(
                    self.discriminative_model_dir / "vocab.json"
                )
                model = model_module.MiniBertClassifier(**config)
                state_dict = torch.load(
                    self.discriminative_model_dir / "model.pt",
                    map_location="cpu",
                    weights_only=True,
                )
                model.load_state_dict(state_dict)
                model.eval()
            except Exception as exc:
                raise ContentModerationError(
                    f"Failed to load local discriminative model: {exc}"
                ) from exc
            self._discriminative_runtime = (torch, tokenizer, config, model)
            return self._discriminative_runtime

    @staticmethod
    def _load_python_module(module_name: str, path: Path) -> Any:
        if not path.is_file():
            raise FileNotFoundError(path)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot import module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _parse_result(self, payload: dict[str, Any]) -> ContentModerationResult:
        data = payload.get("Data") or {}
        results = data.get("Result") or []
        first = results[0] if results and isinstance(results[0], dict) else {}
        code = payload.get("Code")
        risk_level = data.get("RiskLevel")
        label = first.get("Label")
        description = first.get("Description")
        if str(code) != "200":
            message = str(payload.get("Message") or "Aliyun TextModerationPlus request failed")
            request_id = payload.get("RequestId")
            suffix = f" request_id={request_id}" if request_id else ""
            raise ContentModerationError(
                f"{message}{suffix}",
                provider_code=str(code) if code is not None else None,
                request_id=str(request_id) if request_id else None,
            )
        # 当前按控制台验证过的安全返回判断：无风险且无标签才允许继续进入聊天流程。
        allowed = code == 200 and risk_level == "none" and label == "nonLabel"
        return ContentModerationResult(
            allowed=allowed,
            code=code,
            message=str(payload.get("Message") or ""),
            risk_level=risk_level,
            label=label,
            description=description,
            request_id=payload.get("RequestId"),
            raw=payload,
        )

    def _call_text_moderation_plus(
        self,
        text: str,
        service: str,
        *,
        account_id: str | None = None,
        data_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            # 放在函数内部导入，避免未安装内容安全 SDK 时影响整个应用启动。
            from alibabacloud_tea_openapi import models as openapi_models
            from alibabacloud_green20220302.client import Client as GreenClient
            from alibabacloud_green20220302 import models as green_models
        except ImportError as exc:
            raise ContentModerationError(
                "Aliyun TextModerationPlus requires alibabacloud_green20220302 and alibabacloud_tea_openapi"
            ) from exc

        config = openapi_models.Config(
            access_key_id=self.access_key_id,
            access_key_secret=self.access_key_secret,
            endpoint=self.endpoint,
        )
        client = GreenClient(config)
        service_parameters: dict[str, str] = {"content": text}
        if account_id:
            service_parameters["accountId"] = str(account_id)[:64]
        if data_id:
            service_parameters["dataId"] = str(data_id)[:64]
        request = green_models.TextModerationPlusRequest(
            service=service,
            # 阿里云接口要求 ServiceParameters 是 JSON 字符串，不是 Python dict。
            service_parameters=json.dumps(service_parameters, ensure_ascii=False),
        )
        response = client.text_moderation_plus(request)
        body = getattr(response, "body", None)
        if body is None:
            raise ContentModerationError("Aliyun TextModerationPlus response body is empty")
        if hasattr(body, "to_map"):
            return body.to_map()
        if isinstance(body, dict):
            return body
        raise ContentModerationError(f"Unsupported Aliyun response body type: {type(body).__name__}")
