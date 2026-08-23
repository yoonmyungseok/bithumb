FROM python:3.11-slim

# 파이썬 출력 버퍼링 비활성화 및 타임존 설정
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Seoul

WORKDIR /app

# 시스템 패키지 업데이트 및 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스코드 및 설정 디렉토리 복사
COPY src/ ./src/
COPY config/ ./config/

CMD ["python", "src/main.py"]
