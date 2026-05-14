#!/usr/bin/env python3
"""Terminal chat helper for GLM-5.1 with thinking mode enabled by default."""

from __future__ import annotations

import argparse
import base64
import io
import mimetypes
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from zai import ZhipuAiClient


DEFAULT_MODEL = "glm-5.1"
DEFAULT_VISION_MODEL = "glm-5v-turbo"
SUPPORTED_DIRECT_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interact with GLM-5.1 from the terminal.",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Prompt text. If omitted, an interactive session starts.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model name to call. Default: glm-5.1",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        dest="images",
        help=(
            "Local image path to include. Can be passed multiple times. "
            "When set, the request automatically uses --vision-model."
        ),
    )
    parser.add_argument(
        "--vision-model",
        default=DEFAULT_VISION_MODEL,
        help="Vision model used when --image is provided. Default: glm-5v-turbo",
    )
    parser.add_argument(
        "--image-max-edge",
        type=int,
        default=2048,
        help="Resize local images so the longest edge is at most this many pixels. Default: 2048",
    )
    parser.add_argument(
        "--system",
        default="你是一个高效、准确的中文编程助手。",
        help="System prompt.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature. Default: 1.0",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=None,
        help="Optional top_p sampling value.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=4096,
        help="Maximum output tokens. Default: 4096",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("ZAI_API_KEY"),
        help="API key. Defaults to the ZAI_API_KEY environment variable.",
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable thinking mode.",
    )
    parser.add_argument(
        "--show-reasoning",
        action="store_true",
        help="Print reasoning content if the API returns it.",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable streaming output.",
    )
    return parser


def local_image_to_data_url(path: str, max_edge: int) -> str:
    image_path = Path(path).expanduser()
    if not image_path.exists():
        raise SystemExit(f"Image not found: {path}")
    if not image_path.is_file():
        raise SystemExit(f"Image path is not a file: {path}")

    content_type = mimetypes.guess_type(image_path.name)[0]
    if (
        content_type in SUPPORTED_DIRECT_IMAGE_TYPES
        and max_edge <= 0
    ):
        data = image_path.read_bytes()
        return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"

    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - import error path
        raise SystemExit(
            "Missing dependency: Pillow. Run `pip install -r requirements.txt` first."
        ) from exc

    try:
        with Image.open(image_path) as original:
            image = ImageOps.exif_transpose(original)
            image.load()
    except Exception as exc:
        raise SystemExit(f"Could not read image {path}: {exc}") from exc

    if max_edge > 0:
        image.thumbnail((max_edge, max_edge))

    if image.mode not in {"RGB", "RGBA", "L"}:
        image = image.convert("RGB")

    output = io.BytesIO()
    # PNG preserves microscopy labels, scale bars, and line art better than JPEG.
    image.save(output, format="PNG", optimize=True)
    data = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{data}"


def build_user_content(prompt: str, image_paths: list[str], max_edge: int) -> str | list[dict[str, object]]:
    if not image_paths:
        return prompt

    content: list[dict[str, object]] = []
    if prompt:
        content.append({"type": "text", "text": prompt})
    for image_path in image_paths:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": local_image_to_data_url(image_path, max_edge)},
            }
        )
    return content


def select_model(args: argparse.Namespace) -> str:
    if args.images:
        return args.vision_model
    return args.model


def make_client(api_key: str | None) -> ZhipuAiClient:
    if not api_key:
        raise SystemExit(
            "No API key found. Set ZAI_API_KEY or pass --api-key."
        )
    try:
        from zai import ZhipuAiClient
    except ImportError as exc:  # pragma: no cover - import error path
        raise SystemExit(
            "Missing dependency: zai-sdk. Run `pip install zai-sdk==0.2.2` first."
        ) from exc
    return ZhipuAiClient(api_key=api_key)


def emit_stream(chunks: Iterable[object], show_reasoning: bool) -> str:
    final_text: list[str] = []
    reasoning_started = False
    answer_started = False

    for chunk in chunks:
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        delta = choices[0].delta

        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning and show_reasoning:
            if not reasoning_started:
                print("思考：", flush=True)
                reasoning_started = True
            print(reasoning, end="", flush=True)

        content = getattr(delta, "content", None)
        if content:
            if not answer_started:
                if show_reasoning and reasoning_started:
                    print("\n\n回答：", flush=True)
                else:
                    print("回答：", flush=True)
                answer_started = True
            final_text.append(content)
            print(content, end="", flush=True)

    if answer_started:
        print()

    return "".join(final_text)


def one_shot(client: ZhipuAiClient, args: argparse.Namespace, prompt: str) -> str:
    model = select_model(args)
    messages = [
        {"role": "system", "content": args.system},
        {
            "role": "user",
            "content": build_user_content(prompt, args.images, args.image_max_edge),
        },
    ]

    request_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    if args.top_p is not None:
        request_kwargs["top_p"] = args.top_p
    if not args.no_thinking:
        request_kwargs["thinking"] = {"type": "enabled"}

    response = client.chat.completions.create(
        stream=not args.no_stream,
        **request_kwargs,
    )

    if args.no_stream:
        message = response.choices[0].message
        reasoning = getattr(message, "reasoning_content", None)
        if args.show_reasoning and reasoning:
            print("思考：")
            print(reasoning)
            print()
        content = message.content or ""
        print("回答：")
        print(content)
        return content

    return emit_stream(response, args.show_reasoning)


def interactive_session(client: ZhipuAiClient, args: argparse.Namespace) -> None:
    print("GLM-5.1 终端会话已启动，输入 quit / exit 退出。")
    history = [{"role": "system", "content": args.system}]

    while True:
        try:
            user_text = input("你: ").strip()
        except EOFError:
            print()
            break

        if not user_text:
            continue
        if user_text.lower() in {"quit", "exit"}:
            break

        history.append({"role": "user", "content": user_text})

        request_kwargs = {
            "model": args.model,
            "messages": history,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
        }
        if args.top_p is not None:
            request_kwargs["top_p"] = args.top_p
        if not args.no_thinking:
            request_kwargs["thinking"] = {"type": "enabled"}

        response = client.chat.completions.create(
            stream=not args.no_stream,
            **request_kwargs,
        )

        if args.no_stream:
            content = response.choices[0].message.content or ""
            print(f"回答：{content}")
        else:
            content = emit_stream(response, args.show_reasoning)

        history.append({"role": "assistant", "content": content})


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    client = make_client(args.api_key)

    if args.prompt:
        prompt = " ".join(args.prompt).strip()
        if not prompt and not args.images:
            raise SystemExit("Prompt is empty.")
        one_shot(client, args, prompt)
        return

    if args.images and sys.stdin.isatty():
        one_shot(client, args, "请识别并描述图片中的主要信息。")
        return

    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if not prompt and not args.images:
            raise SystemExit("No prompt provided on stdin.")
        one_shot(client, args, prompt)
        return

    interactive_session(client, args)


if __name__ == "__main__":
    main()
