# AI Quant Trading Pro - 모던 웹 대시보드 (Frontend SPA)

## 📌 개요
본 대시보드는 빗썸(Port: 7979) 및 업비트(Port: 7980) AI 퀀트 트레이딩 봇의 실시간 상태를 모니터링하고 원격 제어할 수 있는 모던 SPA(Single Page Application) 대시보드입니다.

## 🚀 주요 기능
1. **멀티 거래소 통합 관제**: 상단 탭에서 [빗썸 (:7979)] / [업비트 (:7980)] 원클릭 전환
2. **4대 핵심 KPI 카드**: 총 평가 자산, 금일 자산 변동(평가 손익), 실현 손익 & 승률, 공포탐욕지수 & BTC 시장 레짐 뱃지
3. **7대 알파 팩터 분석 칩**: 보유 포지션 및 신규 스캔 종목의 7대 팩터 검증 결과 시각화
4. **TradingView 실시간 인터랙티브 차트**: 종목 클릭 시 차트 팝업
5. **원격 비상 제어 (안전 확인 모달 포함)**: 긴급 전량 매도(Panic Sell), 일시정지(Pause), 매매 재개(Resume)
6. **실시간 주문 저널 & 최근 거래 내역 타임라인**

## 💻 실행 및 빌드 방법
### 1) 기본 실행 (Zero Dependency - 무설치 즉시 사용)
별도의 Node.js 설치 없이 봇(`start_bot.bat` 또는 `start_upbit_bot.bat`)을 실행하면 Python 내장 웹서버(`web_server.py`)가 이 `dashboard/` 폴더를 직접 서빙합니다.
- 빗썸 접속: `http://localhost:7979`
- 업비트 접속: `http://localhost:7980`

### 2) 프론트엔드 단독 개발 모드 (Node.js 환경)
```bash
cd dashboard
npm install
npm run dev
# http://localhost:5173 에서 실시간 HMR 개발 가능
```

### 3) 프로덕션 빌드
```bash
npm run build
# dist/ 폴더로 정적 번들 생성
```
