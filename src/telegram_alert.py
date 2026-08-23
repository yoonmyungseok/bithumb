import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class TelegramAlert:
    """
    텔레그램 알림 메시지 발송 및 양방향 인터랙티브 명령어 리스너
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._listener_thread: Optional[threading.Thread] = None
        self._status_callback: Optional[Callable[[], str]] = None
        self._balance_callback: Optional[Callable[[], str]] = None

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """
        텔레그램 방으로 텍스트 메시지 발송
        """
        if not self.bot_token or not self.chat_id:
            logger.info(f"[Telegram 미설정] 메시지 미전송: {text}")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            response = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            if response.status_code == 200:
                logger.info("텔레그램 메시지 전송 성공")
                return True
            else:
                logger.error(
                    f"텔레그램 전송 실패 [{response.status_code}]: {response.text}"
                )
                return False
        except requests.exceptions.RequestException as e:
            logger.error(f"텔레그램 메시지 전송 중 예외 발생: {e}")
            return False

    def start_command_listener(
        self,
        status_callback: Optional[Callable[[], str]] = None,
        balance_callback: Optional[Callable[[], str]] = None,
    ):
        """
        텔레그램 양방향 원격 제어 명령어 리스너 가동 (/status, /balance, /help)
        """
        if not self.bot_token or not self.chat_id:
            return

        self._status_callback = status_callback
        self._balance_callback = balance_callback

        def _listen_loop():
            offset = 0
            logger.info("📱 텔레그램 양방향 인터랙티브 명령어 리스너 가동 시작")
            while True:
                try:
                    url = f"{self.base_url}/getUpdates"
                    params = {"offset": offset, "timeout": 20}
                    res = requests.get(url, params=params, timeout=25)
                    if res.status_code == 200:
                        data = res.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1
                            message = update.get("message", {})
                            text = message.get("text", "").strip()
                            sender_chat_id = str(message.get("chat", {}).get("id", ""))

                            # 인증된 chat_id에서 온 메시지만 처리
                            if sender_chat_id != self.chat_id:
                                continue

                            if not text:
                                continue

                            cmd = text.split()[0].lower()
                            if cmd in ("/status", "/상태", "/state"):
                                if self._status_callback:
                                    reply = self._status_callback()
                                else:
                                    reply = "🟢 빗썸 자동매매 봇이 정상 가동 중입니다."
                                self.send_message(reply)

                            elif cmd in ("/balance", "/잔고", "/자산"):
                                if self._balance_callback:
                                    reply = self._balance_callback()
                                else:
                                    reply = "💰 잔고 조회 중..."
                                self.send_message(reply)

                            elif cmd in ("/help", "/도움말", "/start"):
                                help_msg = (
                                    "🤖 <b>[빗썸 AI 퀀트 봇 원격 제어 명령어]</b>\n\n"
                                    "• <code>/status</code> 또는 <code>/상태</code> : 계좌 종합 대시보드 및 봇 상태 조회\n"
                                    "• <code>/balance</code> 또는 <code>/잔고</code> : 가용 원화 및 보유 코인 실시간 잔고\n"
                                    "• <code>/help</code> 또는 <code>/도움말</code> : 명령어 목록 확인"
                                )
                                self.send_message(help_msg)

                except Exception as e:
                    time.sleep(3)
                time.sleep(1)

        t = threading.Thread(target=_listen_loop, daemon=True, name="TelegramCommandListener")
        t.start()
