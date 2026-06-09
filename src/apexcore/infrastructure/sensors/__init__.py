"""Источники датчиков (температуры) для платформенных адаптеров.

Каждый модуль здесь — самостоятельный источник данных:

- ``lhm`` — внутрипроцессный LibreHardwareMonitorLib через pythonnet (Windows);
- ``wmi_temps`` — WMI/CIM на нативных провайдерах Windows (perf-counter
  Thermal Zone, MSAcpi_ThermalZoneTemperature).

Адаптер ``WindowsAdapter`` оркестрирует их в гибридный pipeline; каждый источник
может быть импортирован и заменён мокой в тестах независимо.
"""
