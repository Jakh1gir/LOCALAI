"""Запуск ИИ-службы: ``python -m ai --key-file <папка базы>/ai.key``.

Служба слушает только этот компьютер (SEC-05) и обращается к серверу по
адресу ``--server`` с делегированным доступом пользователя. Ключ сервера
сервер создаёт сам в файле ``ai.key`` рядом с базой при первом запуске.

Модель: ``--model ollama:<имя>`` (по умолчанию) или ``none``. Если Ollama не
запущен или модели нет, служба работает без модели (AI-12).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from ai import model as models
from ai.service import LOOPBACK, create_app

DEFAULT_PORT = 8731


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m ai", description=__doc__)
    parser.add_argument("--key-file", required=True, help="файл ai.key рядом с базой сервера")
    parser.add_argument("--server", default="http://127.0.0.1:8730", help="адрес сервера LOCALAI")
    parser.add_argument("--cafile", help="сертификат сервера, если он работает по HTTPS")
    parser.add_argument("--host", default="127.0.0.1", help="только локальный адрес")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--model",
        default=models.DEFAULT,
        help="ollama (модель выбирается сама), ollama:<имя>, openai[:<имя>] или none;"
        " администратор меняет модель в настройках ИИ без перезапуска",
    )
    parser.add_argument("--ollama", default=models.OLLAMA_URL, help="адрес Ollama")
    parser.add_argument(
        "--openai-url",
        default="http://127.0.0.1:8080",
        help="адрес OpenAI-совместимого сервера (llama.cpp, vLLM, LM Studio)",
    )
    args = parser.parse_args(argv)
    if args.host not in LOOPBACK:
        sys.exit("Ошибка: ИИ-служба слушает только этот компьютер (127.0.0.1)")
    try:
        key = Path(args.key_file).read_text(encoding="ascii").strip()
        chosen = models.make(args.model, args.ollama, args.openai_url)
    except (OSError, ValueError) as error:
        sys.exit(f"Ошибка: {error}")
    if not key:
        sys.exit("Ошибка: файл ключа пуст")
    app = create_app(server_url=args.server, key=key, model=chosen, cafile=args.cafile)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
