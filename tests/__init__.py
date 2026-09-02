"""tests 패키지 로드 시 Windows SQLite 잠금 해제 훅을 설치한다."""

import os
import sys

# discover -s tests 로드 시에도 훅이 실행되도록 tests 디렉터리를 선등록한다.
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import db_test_cleanup  # noqa: E402, F401
