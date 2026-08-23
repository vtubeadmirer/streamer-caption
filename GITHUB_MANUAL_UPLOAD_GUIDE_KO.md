# GitHub 수동 업로드 안내

이 폴더는 **GitHub 저장소에 올릴 소스 코드용 사본**입니다. 실사용 중인 설정, 유행어 사전, 로그, 녹음, 데이터베이스, 모델/가상환경은 포함하지 않았습니다.

## 권장 방법: GitHub Desktop

파일 수가 많으므로 GitHub 웹사이트에서 하나씩 올리는 것보다 GitHub Desktop이 안전하고 간단합니다.

1. [GitHub Desktop](https://desktop.github.com/)을 설치하고, 사용할 GitHub 계정으로 로그인합니다.
2. GitHub Desktop에서 `File > Add local repository...`를 누릅니다.
3. 이 폴더(`streamer-caption`)를 선택합니다.
4. 변경 파일 목록을 확인한 뒤 Summary에 `Initial source upload`를 입력하고 `Commit to main`을 누릅니다.
5. 상단의 `Publish repository`를 누릅니다.
6. 저장소 이름을 정하고, 첫 업로드는 `Keep this code private`를 켠 비공개 저장소로 게시하는 것을 권장합니다.

이 폴더는 이미 Git 저장소와 `main` 브랜치가 초기화되어 있지만, 커밋과 원격 저장소 연결은 하지 않은 상태입니다. 따라서 다른 GitHub 계정으로 로그인한 뒤 직접 커밋하고 게시할 수 있습니다.

## 릴리스 ZIP은 소스와 분리

사용자에게 배포할 설치 ZIP은 소스 저장소 안에 넣지 말고 GitHub 저장소의 `Releases`에 첨부합니다.

- 소스: 이 폴더의 파일을 저장소에 커밋
- 설치 파일: `streamer-caption-1.0.0-dev.1.zip`을 `Releases`에 첨부
- 릴리스 태그 예시: `v1.0.0-dev.1`

## 공개 전 확인

- 첫 게시 후 GitHub의 파일 목록에서 `config.json`, 로그, 녹음 파일, DB, `.venv`가 없는지 확인합니다.
- 저장소를 공개로 전환하기 전 라이선스를 결정해 `LICENSE` 파일을 추가합니다.
- 실제 스트리머 설정과 유행어는 예제 파일이 아니라 각 사용자의 `user-data`에 보관합니다.

## 브라우저로만 올리는 경우

GitHub 웹의 `Add file > Upload files`로 폴더 안 파일을 업로드할 수도 있지만, 파일 수가 많고 폴더 구조 확인이 불편합니다. ZIP 파일 자체를 업로드해도 GitHub가 소스 트리로 자동 해제하지 않으므로 GitHub Desktop 사용을 권장합니다.
