# Branding pipeline

Источник бренда — `source/apex-logo.png` (512×512 RGBA). Из него на этапе
сборки генерируются все производные ассеты:

| Файл | Назначение |
|---|---|
| `apex-logo.ico` (16/32/48/256) | Иконка `apexcore-setup-*.exe` и `apexcore.exe` (Windows) |
| `apex-logo-256.png` | Иконка `.desktop` на Astra Linux + hicolor 256×256 |
| `apex-logo-128.png` | hicolor 128×128 |
| `apex-logo-64.png`  | hicolor 64×64 |
| `apex-logo-48.png`  | hicolor 48×48 |
| `apex-logo-32.png`  | hicolor 32×32, favicon |
| `apex-logo-52.png`  | StepRail logo в HTML wizard'е (52×52) |
| `apex-logo-80.png`  | Запасной размер (Welcome header в light theme) |

## Запуск генерации

### Windows
```powershell
pwsh -File new-app/scripts/build_branding.ps1
```

### Astra Linux
```bash
bash new-app/scripts/build_branding.sh
```

Оба скрипта пишут в `build/branding/` (рядом с корнем проекта, не в git).
Если ImageMagick отсутствует — скрипт остановится с понятной ошибкой и
ссылкой на установку (`winget install ImageMagick.ImageMagick` /
`apt install imagemagick`).

## Обновление логотипа

1. Замените `source/apex-logo.png` новым PNG (512×512, RGBA, sRGB).
2. Запустите `build_branding.*` — все производные пересоберутся.
3. Закоммитьте новый source PNG; производные (`build/branding/`) **не**
   коммитятся (.gitignore).
