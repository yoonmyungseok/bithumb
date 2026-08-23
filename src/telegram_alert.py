import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

logger = logging.getLogger(__name__)


class TelegramAlert:
    """
    텔레그램 알림 및 실시간 터치형 인라인 버튼 양방향 원격 제어
    - sendMessage & sendPhoto (HTML 서식 및 인라인 키보드 지원)
    - callback_query 실시간 리스너를 통한 스마트폰 원클릭 즉시 제어
    - /status, /balance, /panic, /pause, /resume, /help 및 터치 버튼 인터랙션
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token.strip() if bot_token else ""
        self.chat_id = str(chat_id).strip() if chat_id else ""
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._status_callback: Callable[[], str] | None = None
        self._balance_callback: Callable[[], str] | None = None
        self._panic_callback: Callable[[], str] | None = None
        self._pause_callback: Callable[[], str] | None = None
        self._resume_callback: Callable[[], str] | None = None
        self._buy_approval_callback: Callable[[str], str] | None = None

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """텔레그램 방으로 텍스트 메시지 및 선택적 인라인 버튼 발송"""
        if not self.bot_token or not self.chat_id:
            logger.info(f"[Telegram 미설정] 메시지 미전송: {text}")
            return False

        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            response = requests.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            if response.status_code == 200:
                logger.info("텔레그램 메시지 전송 성공")
                return True
            else:
                logger.error(f"텔레그램 전송 실패 [{response.status_code}]: {response.text}")
                return False
        except requests.exceptions.RequestException as e:
            logger.error(f"텔레그램 메시지 전송 중 예외 발생: {e}")
            return False

    def send_photo(
        self,
        photo_bytes: bytes,
        caption: str = "",
        parse_mode: str = "HTML",
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """텔레그램 방으로 캔들 차트 이미지(PNG) 및 설명 캡션 발송"""
        if not self.bot_token or not self.chat_id or not photo_bytes:
            return False

        url = f"{self.base_url}/sendPhoto"
        data: dict[str, Any] = {
            "chat_id": self.chat_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)

        files = {
            "photo": ("chart.png", photo_bytes, "image/png"),
        }

        try:
            response = requests.post(url, data=data, files=files, timeout=20)
            if response.status_code == 200:
                logger.info("텔레그램 차트 사진 발송 성공")
                return True
            else:
                logger.warning(f"텔레그램 사진 발송 실패 [{response.status_code}]: {response.text}")
                return False
        except requests.exceptions.RequestException as e:
            logger.warning(f"텔레그램 사진 발송 예외: {e}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = ""):
        """인라인 버튼 터치 시 텔레그램 상단 토스트 알림 확인 응답"""
        if not self.bot_token:
            return
        try:
            requests.post(
                f"{self.base_url}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, "text": text},
                timeout=5,
            )
        except requests.exceptions.RequestException:
            pass

    @staticmethod
    def get_dashboard_keyboard() -> dict[str, Any]:
        """텔레그램 메시지 하단 인터랙티브 터치 버튼 키보드 생성"""
        return {
            "inline_keyboard": [
                [
                    {"text": "📊 상태 새로고침", "callback_data": "btn_status"},
                    {"text": "💰 잔고 상세", "callback_data": "btn_balance"},
                ],
                [
                    {"text": "⏸️ 일시정지", "callback_data": "btn_pause"},
                    {"text": "▶️ 재개", "callback_data": "btn_resume"},
                    {"text": "🚨 긴급 전량매도", "callback_data": "btn_panic"},
                ],
            ]
        }

    def start_command_listener(
        self,
        status_callback: Callable[[], str] | None = None,
        balance_callback: Callable[[], str] | None = None,
        panic_callback: Callable[[], str] | None = None,
        pause_callback: Callable[[], str] | None = None,
        resume_callback: Callable[[], str] | None = None,
        buy_approval_callback: Callable[[str], str] | None = None,
    ):
        """텔레그램 양방향 명령어 & 인라인 버튼 콜백 리스너 가동"""
        if not self.bot_token or not self.chat_id:
            return

        self._status_callback = status_callback
        self._balance_callback = balance_callback
        self._panic_callback = panic_callback
        self._pause_callback = pause_callback
        self._resume_callback = resume_callback
        self._buy_approval_callback = buy_approval_callback

        def _listen_loop():
            offset = 0
            logger.info("📱 텔레그램 양방향 인터랙티브 명령어 & 인라인 버튼 리스너 가동 시작")
            while True:
                try:
                    url = f"{self.base_url}/getUpdates"
                    params = {"offset": offset, "timeout": 20}
                    res = requests.get(url, params=params, timeout=25)
                    if res.status_code == 200:
                        data = res.json()
                        for update in data.get("result", []):
                            offset = update["update_id"] + 1

                            # 1. 일반 텍스트 명령어 처리
                            if "message" in update:
                                message = update.get("message", {})
                                text = message.get("text", "").strip()
                                sender_chat_id = str(message.get("chat", {}).get("id", ""))

                                if sender_chat_id != self.chat_id or not text:
                                    continue

                                cmd = text.split()[0].lower()
                                if cmd in ("/status", "/상태", "/state"):
                                    reply = self._status_callback() if self._status_callback else "🟢 봇 정상 가동 중"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cmd in ("/balance", "/잔고", "/자산"):
                                    reply = self._balance_callback() if self._balance_callback else "💰 잔고 조회 중..."
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cmd in ("/panic", "/긴급매도", "/전량매도", "/매도"):
                                    reply = self._panic_callback() if self._panic_callback else "🚨 긴급 매도 실행"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cmd in ("/pause", "/일시정지", "/정지", "/stop"):
                                    reply = self._pause_callback() if self._pause_callback else "⏸️ 봇 일시정지 완료"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cmd in ("/resume", "/재개", "/시작"):
                                    reply = self._resume_callback() if self._resume_callback else "▶️ 봇 자동매매 재개 완료"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cmd in ("/help", "/도움말", "/start"):
                                    help_msg = (
                                        "🤖 <b>[빗썸 AI 퀀트 봇 원격 제어 센터]</b>\n\n"
                                        "• <code>/status</code> 또는 <code>/상태</code> : 종합 대시보드 브리핑\n"
                                        "• <code>/balance</code> 또는 <code>/잔고</code> : 보유 코인별 실시간 잔고/손익\n"
                                        "• <code>/panic</code> 또는 <code>/긴급매도</code> : 🚨 <b>전 코인 즉시 전량 매도 및 100% 현금화</b>\n"
                                        "• <code>/pause</code> 또는 <code>/일시정지</code> : 신규 매매 일시정지 (관망 모드)\n"
                                        "• <code>/resume</code> 또는 <code>/재개</code> : 자동매매 정상 재개\n"
                                        "• <code>/help</code> 또는 <code>/도움말</code> : 명령어 목록 확인\n\n"
                                        "💡 <i>아래 터치 버튼을 직접 눌러서 바로 제어할 수도 있습니다!</i>"
                                    )
                                    self.send_message(help_msg, reply_markup=self.get_dashboard_keyboard())

                            # 2. 인라인 버튼 터치 이벤트 (Callback Query) 처리
                            elif "callback_query" in update:
                                cb = update.get("callback_query", {})
                                cb_id = cb.get("id", "")
                                cb_data = cb.get("data", "")
                                sender_chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))

                                if sender_chat_id != self.chat_id:
                                    continue

                                if cb_data == "btn_status":
                                    self.answer_callback_query(cb_id, "📊 실시간 상태를 갱신합니다...")
                                    reply = self._status_callback() if self._status_callback else "🟢 봇 정상 가동 중"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cb_data == "btn_balance":
                                    self.answer_callback_query(cb_id, "💰 잔고를 조회합니다...")
                                    reply = self._balance_callback() if self._balance_callback else "💰 잔고 조회 중..."
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cb_data == "btn_panic":
                                    self.answer_callback_query(cb_id, "🚨 긴급 전량 매도를 실행합니다!")
                                    reply = self._panic_callback() if self._panic_callback else "🚨 긴급 매도 실행"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cb_data == "btn_pause":
                                    self.answer_callback_query(cb_id, "⏸️ 자동매매를 일시정지했습니다.")
                                    reply = self._pause_callback() if self._pause_callback else "⏸️ 봇 일시정지 완료"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cb_data == "btn_resume":
                                    self.answer_callback_query(cb_id, "▶️ 자동매매를 정상 재개했습니다.")
                                    reply = self._resume_callback() if self._resume_callback else "▶️ 봇 자동매매 재개 완료"
                                    self.send_message(reply, reply_markup=self.get_dashboard_keyboard())

                                elif cb_data.startswith("btn_approve_buy_"):
                                    market = cb_data.replace("btn_approve_buy_", "")
                                    self.answer_callback_query(cb_id, f"✅ {market} 매수를 승인했습니다!")
                                    if self._buy_approval_callback:
                                        res_msg = self._buy_approval_callback(market)
                                        self.send_message(res_msg)

                except requests.exceptions.RequestException as e:
                    logger.debug(f"텔레그램 리스너 네트워크 예외: {e}")
                    time.sleep(3)
                except (ValueError, KeyError) as e:
                    logger.debug(f"텔레그램 리스너 처리 예외: {e}")
                    time.sleep(1)

        t = threading.Thread(target=_listen_loop, daemon=True, name="TelegramListener")
        t.start()
