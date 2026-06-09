#!/usr/bin/env bash
# Контролируемая деградация Linux/Astra для валидации apexcore.
#
# Использование:
#   sudo bash degrade_cpu_linux.sh --max-freq 1500MHz   # ограничить частоту CPU
#   sudo bash degrade_cpu_linux.sh --affinity 2          # оставить только 2 ядра
#   sudo bash degrade_cpu_linux.sh --memory 1G           # ограничить RAM (systemd-run)
#   sudo bash degrade_cpu_linux.sh --noise                # запустить фоновый stress-ng
#   sudo bash degrade_cpu_linux.sh --reset                # снять все ограничения

set -euo pipefail

ACTION=""
VALUE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-freq) ACTION="freq"; VALUE="$2"; shift 2 ;;
    --affinity) ACTION="affinity"; VALUE="$2"; shift 2 ;;
    --memory)   ACTION="memory"; VALUE="$2"; shift 2 ;;
    --noise)    ACTION="noise"; shift ;;
    --reset)    ACTION="reset"; shift ;;
    *) echo "Неизвестный параметр: $1"; exit 2 ;;
  esac
done

case "$ACTION" in
  freq)
    if ! command -v cpupower >/dev/null; then
      echo "cpupower не установлен; sudo apt install linux-tools-generic"
      exit 1
    fi
    echo "Ограничиваю максимальную частоту CPU до ${VALUE}"
    sudo cpupower frequency-set -u "${VALUE}" || true
    ;;
  affinity)
    echo "Оставляю в онлайне ${VALUE} логических ядер; остальные — offline"
    total=$(nproc)
    keep="${VALUE}"
    for ((i = keep; i < total; i++)); do
      echo 0 | sudo tee "/sys/devices/system/cpu/cpu${i}/online" >/dev/null
    done
    ;;
  memory)
    echo "Запустите apexcore под systemd-run с MemoryMax=${VALUE}, например:"
    echo "  systemd-run --user --scope -p MemoryMax=${VALUE} apexcore bench run --profile balanced"
    ;;
  noise)
    if ! command -v stress-ng >/dev/null; then
      echo "stress-ng не найден; sudo apt install stress-ng"
      exit 1
    fi
    n=$(( $(nproc) / 2 ))
    [[ "$n" -lt 1 ]] && n=1
    echo "Запускаю фоновый шум: stress-ng --cpu $n --timeout 600s &"
    nohup stress-ng --cpu "$n" --timeout 600s >/tmp/apexcore_noise.log 2>&1 &
    echo "PID: $!"
    ;;
  reset)
    echo "Снимаю ограничения частоты"
    if command -v cpupower >/dev/null; then
      sudo cpupower frequency-set -u 99GHz || true
    fi
    echo "Включаю обратно все CPU"
    for f in /sys/devices/system/cpu/cpu*/online; do
      echo 1 | sudo tee "$f" >/dev/null || true
    done
    echo "Гашу фоновый stress-ng"
    sudo pkill -f "stress-ng --cpu" || true
    ;;
  *)
    echo "Не указано действие. См. --help в README."
    exit 2
    ;;
esac

echo "Готово."
