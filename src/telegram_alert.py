import logging

import requests

logger = logging.getLogger(__name__)


class TelegramAlert:
    """
    텔레그램 알림 메시지 발송 모듈 (HTML 지원)
    """

    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or not chat_id:
            logger.warning("텔레그램 토큰 또는 Chat ID가 설정되지 않았습니다.")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

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
            response = requests.post(self.base_url, json=payload, timeout=10)
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
