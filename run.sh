#!/bin/bash
# 포켓몬 일본어 공부 툴 실행: ./run.sh   (읽기 멈춘 상태로 시작: ./run.sh --paused)
cd "$(dirname "$0")"
source .venv312/bin/activate
# 30B 모델용 GPU 메모리 한도 (재부팅하면 초기화돼서 매번 확인, 이미 설정돼 있으면 건너뜀)
# ./run.sh --no-llm : no translation model (autopilot runs) -> ~17 GB less memory, no sudo needed
if [[ " $* " != *" --no-llm "* ]] && [ "$(sysctl -n iogpu.wired_limit_mb 2>/dev/null)" != "19456" ]; then
  echo "GPU 메모리 한도 설정 (맥 비밀번호 입력)"
  sudo sysctl iogpu.wired_limit_mb=19456
fi
python fr_study.py live --dev 0 "$@"
