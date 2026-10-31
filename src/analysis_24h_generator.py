"""Groq API를 활용한 수동 매수 종목 24시간 시세 분석 보고서 생성기.

- 수동으로 매수한 종목(봇 자동 매매 포지션 제외)만 선별하여 분석을 수행합니다.
- 거래소 실시간 시세 및 최근 24h/일봉 캔들 데이터를 주입하여 정합성을 보장합니다.
- Groq LLM을 통해 지정된 5개 섹션 마크다운 보고서를 생성하고 '24h/{SYMBOL}_24h_{YYYYMMDD}.md'로 저장합니다.
- 거래소 격리 원칙(업비트/빗썸 키 및 API 분리)을 철저히 준수합니다.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

# 프로젝트 루트 경로
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.groq_provider import GroqProvider
from src.sheets.manual_position_collector import ManualPositionCollector

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


class Analysis24hGenerator:
    """수동 보유 종목 24시간 시세 분석 및 전략 보고서 자동 생성기."""

    def __init__(
        self,
        exchange_scope: str = "all",
        output_dir: str | None = None,
        min_eval_krw: float = 1000.0,
    ) -> None:
        """
        Args:
            exchange_scope: "upbit", "bithumb", 또는 "all" (기본: all)
            output_dir: 보고서 저장 디렉터리 (기본: 프로젝트 루트 '24h')
            min_eval_krw: 최소 평가금액 필터 (1,000원 미만 극소액 제외)
        """
        self.exchange_scope = exchange_scope.lower()
        self.output_dir = os.path.abspath(output_dir or os.path.join(PROJECT_ROOT, "24h"))
        os.makedirs(self.output_dir, exist_ok=True)

        self.collector = ManualPositionCollector(min_eval_krw=min_eval_krw)

        # 거래소별 Groq Provider 분리 초기화 (격리 보장)
        self.upbit_groq = GroqProvider(exchange_scope="upbit")
        self.bithumb_groq = GroqProvider(exchange_scope="bithumb")

    def _get_groq_provider(self, exchange: str) -> GroqProvider:
        """거래소에 맞는 격리된 Groq Provider 반환."""
        if str(exchange).upper() == "UPBIT":
            return self.upbit_groq
        return self.bithumb_groq

    def get_korean_name(self, exchange: str, market: str) -> str:
        """종목 한글명 조회."""
        try:
            if exchange.upper() == "UPBIT" and self.collector.upbit_api:
                return self.collector.upbit_api.get_korean_name(market) or market
            elif exchange.upper() == "BITHUMB" and self.collector.bithumb_api:
                return self.collector.bithumb_api.get_korean_name(market) or market
        except Exception as e:
            logger.debug(f"한글명 조회 예외 ({exchange} {market}): {e}")
        return market.split("-")[-1]

    def fetch_market_data(self, exchange: str, market: str) -> dict[str, Any]:
        """최근 24시간 시세, 거래대금 및 최근 일봉 캔들 데이터 수집."""
        market_upper = market.upper()
        exchange_upper = exchange.upper()

        data: dict[str, Any] = {
            "current_price": 0.0,
            "change_rate": 0.0,
            "change_price": 0.0,
            "high_price_24h": 0.0,
            "low_price_24h": 0.0,
            "acc_trade_price_24h": 0.0,
            "acc_trade_volume_24h": 0.0,
            "daily_candles": [],
        }

        # 1. 티커 데이터 조회
        try:
            if exchange_upper == "UPBIT" and self.collector.upbit_api:
                ticker = self.collector.upbit_api.get_ticker(market_upper, force_refresh=True)
            elif exchange_upper == "BITHUMB" and self.collector.bithumb_api:
                ticker = self.collector.bithumb_api.get_ticker(market_upper, force_refresh=True)
            else:
                ticker = {}

            if ticker:
                data["current_price"] = float(ticker.get("trade_price", 0.0))
                data["change_rate"] = float(ticker.get("signed_change_rate", 0.0)) * 100.0
                data["change_price"] = float(ticker.get("signed_change_price", 0.0))
                data["high_price_24h"] = float(ticker.get("high_price", 0.0))
                data["low_price_24h"] = float(ticker.get("low_price", 0.0))
                data["acc_trade_price_24h"] = float(ticker.get("acc_trade_price_24h", 0.0))
                data["acc_trade_volume_24h"] = float(ticker.get("acc_trade_volume_24h", 0.0))
        except Exception as e:
            logger.warning(f"[{exchange_upper}] {market_upper} 티커 조회 실패: {e}")

        # 2. 최근 5~7일간 일봉 캔들 조회 (공개 REST API 활용)
        try:
            api_base = "https://api.upbit.com/v1" if exchange_upper == "UPBIT" else "https://api.bithumb.com/v1"
            res = requests.get(
                f"{api_base}/candles/days",
                params={"market": market_upper, "count": 7},
                timeout=5.0,
            )
            if res.status_code == 200:
                candles = res.json()
                if isinstance(candles, list):
                    # 과거 -> 최신 순 정렬
                    candles_sorted = list(reversed(candles))
                    daily_list = []
                    for c in candles_sorted:
                        date_str = c.get("candle_date_time_kst", "")[:10]
                        daily_list.append({
                            "date": date_str,
                            "opening_price": float(c.get("opening_price", 0.0)),
                            "high_price": float(c.get("high_price", 0.0)),
                            "low_price": float(c.get("low_price", 0.0)),
                            "trade_price": float(c.get("trade_price", 0.0)),
                            "candle_acc_trade_price": float(c.get("candle_acc_trade_price", 0.0)),
                        })
                    data["daily_candles"] = daily_list
        except Exception as e:
            logger.warning(f"[{exchange_upper}] {market_upper} 일봉 캔들 조회 실패: {e}")

        return data

    def build_prompts(
        self,
        pos: dict[str, Any],
        mkt: dict[str, Any],
        korean_name: str,
        total_account_buy: float,
        now_dt: datetime,
    ) -> tuple[str, str]:
        """Groq에 전달할 시스템 프롬프트 및 사용자 데이터 프롬프트 구성."""
        currency = pos["currency"]
        market = pos["market"]
        exchange = pos["exchange"]
        volume = float(pos.get("volume", 0.0))
        avg_buy_price = float(pos.get("avg_buy_price", 0.0))
        current_price = float(mkt.get("current_price") or pos.get("current_price", 0.0))
        total_buy_price = float(pos.get("total_buy_price", volume * avg_buy_price))
        eval_amount = float(pos.get("eval_amount", volume * current_price))
        unrealized_pnl = float(pos.get("unrealized_pnl", eval_amount - total_buy_price))
        unrealized_pnl_pct = float(pos.get("unrealized_pnl_pct", 0.0))

        # 포트폴리오 비중 산출
        portfolio_weight_pct = (
            (total_buy_price / total_account_buy * 100.0) if total_account_buy > 0 else 100.0
        )

        date_str_kst = now_dt.strftime("%Y년 %m월 %d일 %H:%M")
        sync_time_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        # 일봉 히스토리 요약 텍스트
        daily_lines = []
        for d in mkt.get("daily_candles", []):
            d_date = d["date"]
            d_close = d["trade_price"]
            d_high = d["high_price"]
            d_low = d["low_price"]
            d_val_million = d["candle_acc_trade_price"] / 100_000_000.0
            daily_lines.append(
                f"- {d_date}: 시가 {d['opening_price']:,.2f}원 / 고가 {d_high:,.2f}원 / 저가 {d_low:,.2f}원 / 종가 {d_close:,.2f}원 (거래대금 약 {d_val_million:.1f}억 원)"
            )
        daily_history_text = "\n".join(daily_lines) if daily_lines else "- 일봉 히스토리 데이터 준비 중"

        system_prompt = (
            "당신은 암호화폐 퀀트 투자 분석가이자 엄격한 리스크 관리 전문가입니다. "
            "주어진 실제 시장 팩트 데이터(Fact Sheet)와 사용자의 보유 포지션 데이터를 기반으로, "
            "절대적으로 통일된 고정 마크다운 서식 구조에 맞춰 전문적이고 통찰력 있는 24시간 시세 분석 및 맞춤 대응 전략 보고서를 작성하십시오.\n\n"
            "서식 및 작성 규칙 (반드시 준수):\n"
            "1. 반드시 제공된 팩트 데이터(수량, 평단가, 현재가, 손익률, 거래대금, 지지/저항)를 정확히 인용하고 수치를 임의로 왜곡하거나 조작하지 마십시오.\n"
            "2. 마크다운 제목(#), 대섹션(##), 중제목(###), 표(Table), 번호 매기기(1., 2.) 등 프롬프트에 지정된 마크다운 위계 구조를 한 글자도 변형 없이 정확하게 사용하십시오.\n"
            "   - 특히 '## 4. 보유 평단가 기반 대응 전략 분석' 내부의 소섹션은 반드시 '### 포지션 진단 요약', '### 포지션 핵심 수치 요약 표', '### 포트폴리오 비중 관리 진단', '### 맞춤 대응 및 리스크 관리 지침'과 같이 '###' 헤더 형식을 사용하십시오. ('- **...**:' 글머리 기호 사용 금지)\n"
            "   - '## 2. 향후 24시간 단기 흐름 예측' 내부도 반드시 '### 주요 가격 레벨', '### 예상 시나리오', '### 추세 무효화 기준'과 같이 '###' 헤더 형식을 사용하고 지정된 표(Table) 형식을 준수하십시오.\n"
            "   - '## 3. 단기 / 중기 / 장기 흐름 예측' 역시 지정된 표(Table) 형식을 완벽히 준수하십시오.\n"
            "3. 모든 설명과 분석은 신뢰감 있고 전문적인 어조(한국어 경어체)로 작성하십시오.\n"
            "4. 불필요한 메타 설명(예: '네, 보고서를 작성하겠습니다' 등)은 일절 출력하지 말고, 곧바로 마크다운 1행부터 '# '으로 시작하십시오."
        )

        user_prompt = f"""다음 자산에 대한 '24시간 시세 분석 및 맞춤 대응 전략 보고서'를 아래의 [반드시 준수해야 할 마크다운 보고서 서식 구조]와 100% 동일한 헤더/표/목록 위계 구조로 작성해 주십시오.

[자산 및 포트폴리오 사실 데이터 (FACT SHEET)]
- 작성 기준 일시: {date_str_kst} (KST)
- 거래소: {exchange} 원화 마켓
- 종목명: {korean_name} ({currency})
- 마켓 심볼: {market}
- 실시간 동기화 시각: {sync_time_str}
- 보유 수량: {volume:,.4f} {currency}
- 매수 평단가: {avg_buy_price:,.2f} KRW
- 현재 시장가: {current_price:,.2f} KRW
- 총 매수원금: {total_buy_price:,.0f} KRW
- 현재 평가금액: {eval_amount:,.0f} KRW
- 평가 손익: {unrealized_pnl:+,.0f} KRW ({unrealized_pnl_pct:+.2f}%)
- 계좌 내 매수원금 비중: {portfolio_weight_pct:.2f}%
- 전일 대비 변동: {mkt.get('change_price', 0.0):+,.2f}원 ({mkt.get('change_rate', 0.0):+.2f}%)
- 최근 24시간 변동폭: 저가 {mkt.get('low_price_24h', 0.0):,.2f}원 ~ 고가 {mkt.get('high_price_24h', 0.0):,.2f}원
- 최근 24시간 누적 거래량: {mkt.get('acc_trade_volume_24h', 0.0):,.0f} {currency}
- 최근 24시간 누적 거래대금: {mkt.get('acc_trade_price_24h', 0.0):,.0f} KRW (약 {mkt.get('acc_trade_price_24h', 0.0) / 100_000_000.0:.1f}억 원)

[최근 일별 시세 추이]
{daily_history_text}

[반드시 준수해야 할 마크다운 보고서 서식 구조]
# {korean_name}({currency}) 24시간 시세 분석 및 맞춤 대응 전략 보고서

- 작성 기준 일시: {date_str_kst} (KST)  
- 대상 자산: {exchange} 원화 마켓 {korean_name} ({market})  
- 데이터 출처:  
  - 포트폴리오 데이터: 수동 관리 포트폴리오 실시간 잔고 연동 ({sync_time_str} 기준)  
  - 시장 시세 및 거래량: {exchange} 실시간 시세 및 24시간 호가/체결 데이터  

## 1. {korean_name}({currency}) 최근 24시간 흐름 분석
- **가격 변동 추이 및 전일 대비 변화**
  - 현재가 및 전일 대비 변동, 24시간 변동폭 상세 분석
- **거래량 및 수급 동향**
  - 24시간 누적 거래량, 거래대금 및 수급 체결 강도 분석
- **최근 일별 시세 히스토리 추이 요약**
| 날짜 | 시가 | 고가 | 저가 | 종가 | 거래대금(억 KRW) |
|---|---|---|---|---|---|
(제공된 최근 일별 시세 데이터를 바탕으로 마크다운 표 작성 후 핵심 추이 코멘트)

## 2. 향후 24시간 단기 흐름 예측
### 주요 가격 레벨
| 구분 | 1차 | 2차 | 3차 |
|---|---|---|---|
| **단기 지지선** | (가격 및 근거) | (가격 및 근거) | (가격 및 근거) |
| **단기 저항선** | (가격 및 근거) | (가격 및 근거) | (가격 및 근거) |

### 예상 시나리오
| 시나리오 | 확률 | 전개 내용 | 목표 가격대 |
|---|---|---|---|
| **기본 시나리오** (60%) | 60% | 구체적 전개 예측 | 예상 범위 |
| **상방 시나리오** (25%) | 25% | 돌파 트리거 및 전개 | 목표 가격대 |
| **하방 시나리오** (15%) | 15% | 지지 이탈 시 전개 | 지지 가격대 |

### 추세 무효화 기준
- **상방 무효**: 구체적 무효화 기준선 및 조건
- **하방 무효**: 구체적 무효화 기준선 및 조건

## 3. 단기 / 중기 / 장기 흐름 예측
| 기간 | 전망 | 주요 근거 |
|---|---|---|
| **단기 (1주 ~ 1개월)** | 전망 기술 | 주요 근거 기술 |
| **중기 (1개월 ~ 6개월)** | 전망 기술 | 주요 근거 기술 |
| **장기 (6개월 ~ 1년 이상)** | 전망 기술 | 주요 근거 기술 |

## 4. 보유 평단가 기반 대응 전략 분석
### 포지션 진단 요약
- **보유 수량**: {volume:,.4f} {currency}  
- **매수 평단가**: {avg_buy_price:,.2f} KRW  
- **현재가**: {current_price:,.2f} KRW  
- **총 매수원금**: {total_buy_price:,.0f} KRW  
- **현재 평가금액**: {eval_amount:,.0f} KRW  
- **평가 손익**: {unrealized_pnl:+,.0f} KRW ({unrealized_pnl_pct:+.2f}%)  

### 포지션 핵심 수치 요약 표
| 구분 | 수치 / 내용 | 비고 |
| :---- | :---- | :---- |
| 보유 수량 | {volume:,.4f} {currency} | 현재 잔고 |
| 매수 평단가 | {avg_buy_price:,.2f} KRW | 평균 매수 단가 |
| 현재가 | {current_price:,.2f} KRW | 현재 시장가 |
| 총 매수원금 | {total_buy_price:,.0f} KRW | 초기 투자 원금 |
| 현재 평가금액 | {eval_amount:,.0f} KRW | 현재 평가 가치 |
| 평가 손익 | {unrealized_pnl:+,.0f} KRW | 손익률: {unrealized_pnl_pct:+.2f}% |
| 계좌 내 비중 | {portfolio_weight_pct:.2f}% | 전체 포트폴리오 대비 |

### 포트폴리오 비중 관리 진단
- 비중 및 리스크 집중도에 대한 정밀 진단 내용

### 맞춤 대응 및 리스크 관리 지침
1. **추가 매수(물타기) 지침**
   - 비중 과다 시 엄격한 제한 또는 금지 기준
2. **1차 분할 매도 목표가 및 실행 권고**
   - 목표가 및 매도 비중 권고
3. **2차 분할 매도 목표가**
   - 2차 목표가 및 실행 비중
4. **본전 탈출 목표가 ({avg_buy_price:,.2f} KRW)**
   - 평단가 도달 시 탈출/비중 축소 전략
5. **손익 방어선 (트레일링 스탑 / 손절선)**
   - 구체적 손절 기준 및 트레일링 스탑 가격

## 5. 보고서 핵심 요약
- **시세 흐름**: 핵심 요약
- **핵심 가격대**: 주요 지지선 및 저항선
- **포지션 전략**: 대응 액션 플랜 요약
- **주요 리스크**: 주의해야 할 위험 요인

위 헤더와 표 양식을 반드시 그대로 유지하여 일관된 마크다운 보고서로 출력하십시오."""

        return system_prompt, user_prompt

    def generate_report_for_position(
        self,
        pos: dict[str, Any],
        total_account_buy: float,
        now_dt: datetime | None = None,
    ) -> str | None:
        """단일 수동 종목에 대한 분석 보고서를 Groq API로 생성."""
        now_dt = now_dt or datetime.now(KST)
        exchange = pos["exchange"]
        currency = pos["currency"]
        market = pos["market"]

        korean_name = self.get_korean_name(exchange, market)
        logger.info(f"[{exchange}] {currency} ({korean_name}) 시세 데이터 수집 중...")

        mkt = self.fetch_market_data(exchange, market)
        system_prompt, user_prompt = self.build_prompts(pos, mkt, korean_name, total_account_buy, now_dt)

        provider = self._get_groq_provider(exchange)
        logger.info(f"[{exchange}] {currency} Groq API 분석 요청 시작...")

        result = provider.complete_text(
            prompt=user_prompt,
            system_instruction=system_prompt,
            temperature=0.3,
            max_tokens=4096,
            timeout=25.0,
        )

        if not result.success or not result.value:
            logger.error(f"[{exchange}] {currency} Groq 보고서 생성 실패: {result.error_kind}")
            return None

        content = str(result.value).strip()
        # 혹시 모를 코드 블록 래핑(```markdown ... ```) 제거
        if content.startswith("```markdown"):
            content = content[len("```markdown"):].strip()
        elif content.startswith("```"):
            content = content[len("```"):].strip()
        if content.endswith("```"):
            content = content[:-3].strip()

        logger.info(f"[{exchange}] {currency} Groq 보고서 생성 성공 (모델: {result.model}, 소요: {result.latency_sec:.2f}s)")
        return content

    def save_report(self, currency: str, content: str, date_str: str) -> str:
        """보고서 내용을 원자적으로 파일에 저장."""
        filename = f"{currency.upper()}_24h_{date_str}.md"
        target_path = os.path.join(self.output_dir, filename)

        # 원자적 쓰기 (임시 파일 생성 후 rename)
        temp_dir = self.output_dir
        fd, temp_path = tempfile.mkstemp(dir=temp_dir, prefix=f".{filename}_", suffix=".tmp")
        try:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            os.replace(temp_path, target_path)
            logger.info(f"✅ 24h 보고서 저장 완료: {target_path}")
            return target_path
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def get_target_positions(self) -> list[dict[str, Any]]:
        """설정된 exchange_scope에 따라 대상 수동 종목 목록 수집."""
        if self.exchange_scope == "upbit":
            return self.collector.collect_upbit_manual_positions()
        elif self.exchange_scope == "bithumb":
            return self.collector.collect_bithumb_manual_positions()
        else:
            return self.collector.get_all_manual_positions()

    def run(
        self,
        target_currency: str | None = None,
        now_dt: datetime | None = None,
    ) -> list[str]:
        """
        수동 매수 종목들을 순회하며 24h 분석 보고서를 생성하고 저장합니다.
        
        Args:
            target_currency: 특정 종목 심볼만 지정할 경우 (예: 'HOLO', None이면 전체)
            now_dt: 기준 일시 (기본: 현재 시각 KST)
        
        Returns:
            생성되어 저장된 파일 경로 리스트
        """
        now_dt = now_dt or datetime.now(KST)
        date_str = now_dt.strftime("%Y%m%d")

        logger.info(f"🚀 [Analysis24hGenerator] 24h 수동 종목 분석 작업 시작 (대상 거래소: {self.exchange_scope}, 기준일: {date_str})")

        positions = self.get_target_positions()
        if not positions:
            logger.info("ℹ️ 현재 수동 매수된 보유 종목이 없습니다.")
            return []

        # 총 매수원금 합산 (비중 산출용)
        total_account_buy = sum(float(p.get("total_buy_price", 0.0)) for p in positions)

        saved_files: list[str] = []
        for idx, pos in enumerate(positions):
            currency = pos["currency"].upper()
            if target_currency and currency != target_currency.upper():
                continue

            logger.info(f"▶️ [{idx+1}/{len(positions)}] {pos['exchange']} {currency} 분석 진행 중...")
            try:
                report_content = self.generate_report_for_position(
                    pos=pos,
                    total_account_buy=total_account_buy,
                    now_dt=now_dt,
                )
                if report_content:
                    saved_path = self.save_report(currency, report_content, date_str)
                    saved_files.append(saved_path)
            except Exception as e:
                logger.error(f"❌ {pos['exchange']} {currency} 보고서 생성 중 예외 발생: {e}", exc_info=True)

            # Groq API 호출 간격 제어 (Rate Limit 보호용 2초 휴식)
            if idx < len(positions) - 1:
                time.sleep(2.0)

        logger.info(f"✨ [Analysis24hGenerator] 작업 완료: 총 {len(saved_files)}개 보고서 저장됨")
        return saved_files
