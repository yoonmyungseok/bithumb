import logging
import threading
import time
from collections.abc import Callable

import requests

logger = logging.getLogger(__name__)


class TelegramAlert:
    """
    텔레그램 알림 메시지 발송 및 양방향 인터랙티브 원격 제어 리스너
    - /status, /상태 : 실시간 계좌 대시보드
    - /balance, /잔고 : 보유 코인 및 원화 잔고
    - /panic, /긴급매도 : 전 종목 즉시 시장가 전량 매도 및 100% 현금화
    - /pause, /일시정지 : 봇 자동매매 일시정지 (신규 매수 차단)
    - /resume, /재개 : 봇 자동매매 정상 재개
    - /help, /도움말 : 명령어 안내
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip()
        self.chat_id = str(chat_id).strip()
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._listener_thread: threading.Thread | None = None
        self._status_callback: Callable[[], str] | None = None
        self._balance_callback: Callable[[], str] | None = None
        self._panic_callback: Callable[[], str] | None = None
        self._pause_callback: Callable[[], str] | None = None
        self._resume_callback: Callable[[], str] | None = None

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
        status_callback: Callable[[], str] | None = None,
        balance_callback: Callable[[], str] | None = None,
        panic_callback: Callable[[], str] | None = None,
        pause_callback: Callable[[], str] | None = None,
        resume_callback: Callable[[], str] | None = None,
    ):
        """
        텔레그램 양방향 원격 제어 명령어 리스너 가동
        """
        if not self.bot_token or not self.chat_id:
            return

        self._status_callback = status_callback
        self._balance_callback = balance_callback
        self._panic_callback = panic_callback
        self._pause_callback = pause_callback
        self._resume_callback = resume_callback

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

                            elif cmd in ("/panic", "/긴급매도", "/전량매도", "/매도"):
                                if self._panic_callback:
                                    reply = self._panic_callback()
                                else:
                                    reply = "🚨 긴급 매도 핸들러가 등록되지 않았습니다."
                                self.send_message(reply)

                            elif cmd in ("/pause", "/일시정지", "/정지", "/stop"):
                                if self._pause_callback:
                                    reply = self._pause_callback()
                                else:
                                    reply = "⏸️ 봇이 일시정지되었습니다."
                                self.send_message(reply)

                            elif cmd in ("/resume", "/재개", "/시작"):
                                if self._resume_callback:
                                    reply = self._resume_callback()
                                else:
                                    reply = "▶️ 봇 자동매매가 재개되었습니다."
                                self.send_message(reply)

                            elif cmd in ("/help", "/도움말", "/start"):
                                help_msg = (
                                    "🤖 <b>[빗썸 AI 퀀트 봇 원격 제어 명령어]</b>\n\n"
                                    "• <code>/status</code> 또는 <code>/상태</code> : 종합 대시보드 & 공포탐욕지수 브리핑\n"
                                    "• <code>/balance</code> 또는 <code>/잔고</code> : 보유 코인별 실시간 잔고/손익\n"
                                    "• <code>/panic</code> 또는 <code>/긴급매도</code> : 🚨 <b>전 코인 즉시 전량 매도 및 100% 현금화</b>\n"
                                    "• <code>/pause</code> 또는 <code>/일시정지</code> : 신규 매매 일시정지 (관망 모드)\n"
                                    "• <code>/resume</code> 또는 <code>/재개</code> : 자동매매 정상 재개\n"
                                    "• <code>/help</code> 또는 <code>/도움말</code> : 명령어 목록 확인"
                                )
                                self.send_message(help_msg)

                except (requests.exceptions.RequestException, KeyError, ValueError):
                    time.sleep(3)
                time.sleep(1)

        t = threading.Thread(target=_listen_loop, daemon=True, name="TelegramCommandListener")
        t.start()
