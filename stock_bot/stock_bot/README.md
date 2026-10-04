# 주식 AI 비서 봇 (설치)
1. Python 3.10+ 설치
2. 텔레그램 @BotFather → /newbot → 토큰 복사. 봇에게 아무 말이나 보낸 뒤 @userinfobot 으로 내 chat id 확인
3. https://console.anthropic.com 에서 API 키 발급
4. `pip install -r requirements.txt`
5. `.env.example` 을 `.env` 로 복사해 값 채우기
6. `python bot.py` 실행 → 텔레그램에서 /start
* 매일 08:00(KST)에 PC가 켜져 있고 bot.py가 실행 중이어야 합니다.
  (Windows: 작업 스케줄러로 '로그온 시 python bot.py' 등록 / 절전 모드 해제)
