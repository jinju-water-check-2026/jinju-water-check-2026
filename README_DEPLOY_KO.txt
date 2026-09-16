2026년 진주지사 관내 3종 수중기초점검 - Render 배포 안내

1. GitHub 저장소 루트에 다음 파일을 올립니다.
   index.html
   server.py
   requirements.txt
   render.yaml
   .python-version
   README_DEPLOY_KO.txt

2. Render > Environment에서 KMA_SERVICE_KEY에 기상청 API허브 인증키를 입력합니다.
   - 인증키를 GitHub/ZIP에 넣지 마세요.
   - 저장 시 'Save, rebuild, and deploy'를 선택하세요.

3. DATA_GO_KR_SERVICE_KEY는 단기예보 기능을 계속 사용할 경우에만 입력합니다.
   - 기상청 API허브 인증키와 data.go.kr 인증키는 서로 다른 인증 체계입니다.
   - AWS 일자료/실황 기능은 KMA_SERVICE_KEY만으로 동작하도록 수정되었습니다.

4. /api/kma/status는 외부 기상청 호출을 직접 하지 않고 서버와 인증키 설정 여부만 확인합니다.
   따라서 Render Health Check가 기상청 일시 장애 때문에 실패하지 않습니다.

5. AWS 일자료는 기상청 API허브의 sfc_aws_day.php를 사용하며 rn_day(일강수량)를 조회합니다.
   대표 지점: 금남 AWS 933, 사천 AWS 917.
