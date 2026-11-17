# Church Bulletin Editor

MIT-licensed source for a local Korean–Japanese church bulletin editor.

주보에서 바뀐 부분만 수정하고 A4 양면 소책자로 출력하는 로컬 편집기입니다. 입력 항목 버튼, 직접 글 수정, 내용 보존형 페이지 전체 정돈과 예배 PPT 재사용 연결을 제공합니다. 현재 제작 중인 교회별 템플릿을 사용하는 도구이며 모든 교회에 바로 맞는 완제품은 아닙니다.

## 클릭 선택 입력

설교 제목과 본문을 한 창에서 수정합니다. 말씀은 구약/신약·성경 책·장·절 범위를 클릭해 선택하며, 장·절 메타데이터는 사용자가 등록한 로컬 성경 데이터에서 읽습니다. 한국어 원문을 확인한 뒤 일본어 원문을 입력하거나 나중에 입력한다고 명시적으로 선택합니다. 새 구절을 적용할 때 이전 일본어 본문을 무심코 남기지 않습니다. 찬송가·찬양은 로컬 승인 라이브러리 색인에서 번호·곡명을 검색하고 클릭해 선택합니다. 성경 본문과 라이브러리 색인은 공개 저장소에 포함하지 않습니다.

## ChatGPT 계정 연결

`Continue with ChatGPT / ChatGPT로 계속하기`는 OpenAI의 공식 로컬 OAuth/OIDC 흐름을 사용합니다. 사용자가 로그인하고 승인한 앱 사용 권한만 연결하며 비밀번호, 쿠키, 다른 앱의 토큰을 가져오지 않습니다. 공개 Responses API에 `store:false`, `stream:true`로 요청합니다.

공식 로그인 후 계정에 실제로 부여된 AI 사용 권한과 사용 가능한 모델을 확인합니다. 무료 계정의 지원 여부나 추가 사용량을 보장하지 않습니다. 계정·권한·사용량이 허용하지 않아도 기본 편집·인쇄·측정 기반 자동 정돈은 이용할 수 있습니다. AI 실패 시 다른 유료 API로 자동 전환하지 않습니다.

토큰은 프로젝트나 OneDrive 폴더가 아닌 Windows 사용자의 `%LOCALAPPDATA%/ChurchBulletinEditor/ChatGPT`에 DPAPI로 암호화해 보관합니다. 요청은 사용하는 사람의 로컬 PC에서 실행합니다. 사용자마다 본인 계정을 사용하며 공동 서버에서 다른 사람의 구독을 공유하는 용도로 제공하지 않습니다.

공식 근거: [Sign in with ChatGPT](https://developers.openai.com/siwc/quickstart), [로컬 앱 로그인](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [계정 관리](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions), [AI 호출 제한](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).

## 로컬 실행

Windows, Python 3.12+, Poppler(`pdftoppm`), 시스템 한글·일본어 글꼴이 필요합니다.

```powershell
python -m pip install -r apps/bulletin-editor/requirements.txt
python apps/bulletin-editor/server.py --port 8766
```

현재 교회 전용 조판기는 로컬의 `자료/주보_기준원본` 안에 사용 권한을 가진 기준 PDF 두 개(파일명이 각각 `20261108`, `20261115`로 시작하는 4면/8면 파일)를 필요로 합니다. 현재 사용 중인 비공개 프로젝트에 이 소스 변경을 적용하면 기존 로컬 자료를 그대로 이용합니다. 이 공개 저장소에는 해당 PDF를 제공하지 않습니다. 말씀·찬송가·템플릿·PPT 라이브러리를 사용하는 기능도 사용자가 별도로 권한 있는 자료를 준비해야 합니다.

라이선스 원문이 없는 사람은 성경 데이터를 임의로 수집하거나 포함하지 마세요. 이 프로그램의 MIT 라이선스가 성경·음악·글꼴·교회 문서의 사용 허락을 대신하지 않습니다.

접속 주소는 `http://127.0.0.1:8766/`이며 서버는 로컬 주소에만 바인딩됩니다. 이 저장소 자체가 온라인 예배 편집 서비스를 호스팅하지는 않습니다.

## 개인정보와 공개 범위

공개 파일은 프로그램 코드, 라이선스, 설치 설명 및 실제 데이터가 없는 인증 테스트로 제한합니다. 주보/PDF, 성경 JSON, 찬송가·악보·PPT, 글꼴 파일, 교회 연락처/봉사 명단/담당자 정보, 작업 파일, 생성 결과물, API 키 및 로그인 정보는 포함하지 않습니다. `PUBLICATION_MANIFEST.json`에는 공개 파일 목록과 SHA-256만 기록합니다.

이전 영역 편집기와 교회별 배포·설치 자료는 공개 소스 묶음에 포함하지 않습니다. 원본 사용자의 비공개 프로젝트에는 그대로 남아 있습니다.

## 인증 검증

```powershell
python -m unittest discover -s apps/bulletin-editor -p test_siwc.py
```

테스트는 자체 생성 서명과 가짜 응답만 사용하며 실제 계정이나 OpenAI 서버에 접속하지 않습니다. 실제 로그인은 사용자가 공식 브라우저 화면에서 완료합니다.
