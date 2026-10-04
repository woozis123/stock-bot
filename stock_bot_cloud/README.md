# 주식 AI 비서 (GitHub Actions판 - PC 꺼도 됨)
## 설정 (한 번만)
1. 텔레그램 @BotFather → /newbot → 토큰 복사. 봇에게 아무 말이나 보낸 뒤 @userinfobot 으로 내 chat id 확인
2. console.anthropic.com 에서 Claude API 키 발급
3. GitHub에서 **Private** 저장소 생성 → 이 폴더 전체 업로드 (.github 폴더 포함)
4. 저장소 Settings → Secrets and variables → Actions → New repository secret 3개 등록
   TELEGRAM_TOKEN / TELEGRAM_CHAT_ID / ANTHROPIC_API_KEY
5. Settings → Actions → General → Workflow permissions → "Read and write permissions"
6. Actions 탭 → stock-bot → Run workflow(mode=report)로 테스트
## 사용 (폰 텔레그램에서 봇에게 메시지)
- "SK하이닉스 190000원에 10주 샀음" / "엔비디아 120달러 5주 매수"
- "하이닉스 팔았음 210000"
- "섹터 추가 2차전지" / "섹터 삭제 반도체"
→ 21:00(저녁)과 08:00(아침) 실행 때 반영되고 확인 메시지가 옵니다.
(바로 반영하려면 Actions 탭에서 Run workflow, mode=sync)
