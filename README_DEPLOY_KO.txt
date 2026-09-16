2026 진주지사 3종 수중기초점검 - Render 웹배포용

[중요]
- 실제 공공데이터포털 인증키는 이 ZIP/깃허브에 넣지 마세요.
- Render 배포 화면에서 KMA_SERVICE_KEY 환경변수 값으로만 입력하세요.
- 인증키 계정에는 다음 2개 API 활용승인이 필요합니다.
  1) 기상청_자동기상관측(AWS) 조회서비스
  2) 기상청_단기예보 조회서비스

[배포 순서]
1. GitHub에서 새 저장소를 만듭니다. 예: jinju-water-check
2. 이 ZIP을 풀고, 폴더 안의 파일 6개를 GitHub 저장소에 업로드합니다.
   - index.html
   - server.py
   - requirements.txt
   - render.yaml
   - .python-version
   - README_DEPLOY_KO.txt
3. Render에서 New > Blueprint를 선택하고 방금 만든 GitHub 저장소를 연결합니다.
4. Render가 KMA_SERVICE_KEY 값을 요구하면 공공데이터포털 인증키 전체를 붙여넣습니다.
5. 배포가 완료되면 https://<서비스명>.onrender.com 형태의 링크가 생성됩니다.
6. 그 링크 하나만 다른 직원에게 전달하면 됩니다. 상대방은 Python 설치가 필요 없습니다.

[설정]
- Render 서비스명 기본값: jinju-water-check
- Build Command: pip install -r requirements.txt
- Start Command: gunicorn server:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
