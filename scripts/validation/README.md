# Валидационный стенд apexcore

Сценарии контролируемой деградации на ВМ для проверки чувствительности
стат-движка диагностики.

## Идея

1. Прогнать «здоровый» бенчмарк на чистой ВМ → сохранить как baseline.
2. Применить один из сценариев деградации (см. ниже).
3. Прогнать бенчмарк ещё раз.
4. Запустить `apexcore compare <baseline_id> <current_id>` и `apexcore diagnose
   <current_id> --baseline <baseline_id>` — стат-движок должен **детектировать**
   соответствующую категорию деградации с заявленной долей правильных ответов.
5. Повторить для всех сценариев и собрать отчёт.

## Сценарии

| Имя             | ОС                | Эффект                                | Скрипт                                  |
|-----------------|-------------------|---------------------------------------|-----------------------------------------|
| cpu_limit       | Linux/Astra       | ограничение частоты CPU через cpufreq | `degrade_cpu_linux.sh`                  |
| cpu_affinity    | Linux/Astra       | прогон в 2 ядрах через `taskset`      | `degrade_cpu_linux.sh --affinity 2`     |
| ram_limit       | Linux/Astra       | ограничение RAM systemd-cgroup        | `degrade_cpu_linux.sh --memory 1G`      |
| background_load | оба               | фоновый шум `stress-ng --cpu N/2`     | `degrade_cpu_linux.sh --noise`          |
| cpu_limit_win   | Windows 11        | максимум 50% CPU через `powercfg`     | `degrade_cpu_windows.ps1`               |
| affinity_win    | Windows 11        | старт apexcore через `start /AFFINITY`| `degrade_cpu_windows.ps1 -Affinity 0x3` |

## Запуск

```bash
# 1. baseline
apexcore bench run --profile balanced --duration 60
# скопировать UUID из вывода

# 2. деградация (на Linux):
sudo bash scripts/validation/degrade_cpu_linux.sh --max-freq 1500MHz

# 3. повторный прогон
apexcore bench run --profile balanced --duration 60

# 4. сравнение
apexcore compare <baseline_uuid> <current_uuid> --alpha 0.05
apexcore diagnose <current_uuid> --baseline <baseline_uuid>

# 5. снять ограничение
sudo bash scripts/validation/degrade_cpu_linux.sh --reset
```

Полный автоматизированный прогон — в `run_validation.py`.

## Отчёт

Скрипт `run_validation.py` сохраняет JSON-отчёт в `validation_report.json`:

```json
{
  "scenarios": [
    {
      "name": "cpu_limit",
      "expected": "cpu_int_degradation",
      "detected_codes": ["cpu_int_degradation", "cpu_thermal_throttle_warning"],
      "passed": true
    }
  ],
  "accuracy": 0.83
}
```
