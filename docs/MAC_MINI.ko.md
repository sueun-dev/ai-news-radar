# Mac mini로 이전하기

이 저장소에는 프로그램 소스와 공개 소스 목록만 들어 있습니다. 기존 수집 데이터·로그·개인 X 설정·인증 정보·기존 Mac의 앱 바이너리는 포함하지 않습니다. Mac mini에서 알림 앱을 새로 빌드합니다.

## 1. 기존 Mac에서 비공개 백업

새 공개 저장소를 clone한 폴더에서 실행해도 현재 설치 위치의 DB를 읽습니다. 수집기를 끌 필요 없이 SQLite backup API로 일관된 스냅샷을 만듭니다.

```sh
git clone https://github.com/sueun-dev/ai-news-radar.git
cd ai-news-radar
python3 manage.py backup --output ../ai-news-radar-private.zip
```

ZIP은 수집 이력·설정·알림 선호를 포함하는 개인 파일입니다. AirDrop, 외장 디스크 또는 본인이 신뢰하는 비공개 전송 수단으로 옮기세요. GitHub 저장소·이슈·공개 첨부에는 올리지 마세요. 브라우저 로그인·X 푸시 권한은 백업에 포함되지 않습니다.

## 2. Mac mini 준비

macOS 13 이상, Python 3.9 이상, SQLite 3.25 이상이 필요합니다. 알림 앱을 쓰려면 Apple Command Line Tools가 필요합니다.

```sh
xcode-select --install
```

설치 창에서 완료한 뒤 다음을 실행합니다. 이미 설치돼 있다는 응답이면 다시 설치할 필요 없습니다.

```sh
git clone https://github.com/sueun-dev/ai-news-radar.git
cd ai-news-radar
python3 -m unittest discover -s tests -v
python3 manage.py restore --input /실제/백업/경로/ai-news-radar-private.zip
python3 manage.py install --keep-awake
python3 manage.py status
```

예전 이력이 필요 없다면 `restore`만 생략하세요. 기존 대상 DB가 있으면 복원은 거부됩니다. 그 DB를 지우지 말고 먼저 따로 백업·보존하세요.

설치하면 로그인 시 자동 시작하는 수집기와 알림 도우미가 등록됩니다. `--keep-awake`는 실행 중 유휴 시스템 잠자기를 막습니다. **로그인한 사용자 세션·인터넷·전원이 필요**하며 재부팅 후에도 사용자 로그인이 필요합니다. 화면이 꺼지는 것은 수집 중단과 다릅니다. 강제 잠자기·종료·전원 중단 상태에서는 수집할 수 없습니다.

## 3. 이 Mac에서 동작 확인

1. Mac mini에서 <http://127.0.0.1:8741/>을 엽니다.
2. `manage.py status`의 `health.running`, `poller_alive`가 true인지 확인합니다.
3. 첫 수집에는 시간이 걸립니다. 정상 소스 수와 마지막 정상 시각을 확인하고, 오류는 해당 소스의 응답과 재시도 상태를 보세요.
4. macOS가 요청하면 **AI News Radar** 알림을 허용합니다. 시스템 설정 → 알림에서도 허용 상태를 확인합니다.
5. 화면의 ‘시험 알림’을 누릅니다. ‘macOS 접수’와 실제 배너 표시를 각각 확인합니다.
6. X 공식 알림은 이 Mac의 X·브라우저에서 따로 설정합니다. Mac mini의 로컬 macOS 알림이 다른 Mac이나 휴대폰으로 자동 전달되는 구성은 아닙니다.

복원은 이전의 미처리 알림을 재전송하지 않도록 기록만 남기고, 새 Mac의 알림 권한은 새로 확인합니다. 기존 이력의 관측 공백은 보존합니다.

## 4. 확인 후 기존 Mac의 수집기 끄기

Mac mini의 동작을 확인한 다음, **기존 Mac에서** 실행합니다.

```sh
python3 manage.py backup --output ../ai-news-radar-final-private.zip
python3 manage.py disable
```

`disable`은 이 프로그램의 로그인 시작 작업을 제거하며 수집 DB는 보존합니다. 두 Mac을 동시에 계속 실행하면 서로 독립된 수집과 알림이 생깁니다. 마지막 백업 이후 생긴 두 DB를 자동으로 합치는 기능은 없습니다.

## 5. 업데이트

```sh
git pull --ff-only
python3 -m unittest discover -s tests -v
python3 manage.py install --keep-awake
python3 manage.py status
```

기존 설치 설정과 DB를 유지합니다. 설정을 바꾸려면 `install --config /실제/경로/config.json --keep-awake`로 명시하세요. 설치 옵션은 업데이트 때도 다시 지정합니다. 새 프로그램의 준비 상태 확인이 실패하면 이전 코드와 LaunchAgent 설정으로 복구를 시도하고 이력은 유지합니다. 새 기기에서의 실제 설치·재부팅·배너 수신은 해당 기기에서 확인해야 합니다.
